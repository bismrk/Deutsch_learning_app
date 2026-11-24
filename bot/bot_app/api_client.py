"""Typed async HTTP client for communicating with the Backend API."""

from __future__ import annotations

import logging
import math
from typing import Any, Final, TypeVar, overload

import httpx
from pydantic import BaseModel, ValidationError

from bot_app.config import DEFAULT_BACKEND_API_TIMEOUT_SECONDS
from bot_app.schemas import (
    ActiveSessionResponse,
    RadarStatsResponse,
    SessionAbandonResponse,
    SessionAnswerResponse,
    SessionHintResponse,
    SessionMode,
    SessionStartResponse,
    UserLevel,
    UserProfile,
    UserTheme,
    WordImportResult,
    WordList,
)

logger = logging.getLogger(__name__)

_ResponseModelT = TypeVar("_ResponseModelT", bound=BaseModel)


class _UnsetType:
    """Sentinel distinguishing omitted PATCH fields from explicit ``None`` values."""


_UNSET: Final = _UnsetType()

RATE_LIMIT_MESSAGE_UA: Final = (
    "Денний ліміт запитів до AI-тренера вичерпано. Спробуй ще раз завтра або трохи пізніше."
)
AUTH_ERROR_MESSAGE_UA: Final = (
    "Помилка авторизації сервісу. Спробуй пізніше або звернися до адміністратора."
)
TIMEOUT_ERROR_MESSAGE_UA: Final = (
    "Сервіс відповідає занадто довго. Спробуй ще раз за хвилину."
)
UNAVAILABLE_ERROR_MESSAGE_UA: Final = (
    "Сервіс тимчасово недоступний. Спробуй ще раз трохи пізніше."
)
CLIENT_ERROR_FALLBACK_MESSAGE_UA: Final = (
    "Не вдалося виконати запит. Перевір дані та спробуй ще раз."
)


class BackendAPIError(Exception):
    """Base typed exception raised by ``BackendAPIClient`` without leaking secrets."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)

    @property
    def user_message(self) -> str:
        """Return a safe, user-facing Ukrainian message."""
        return self.message


class RateLimitError(BackendAPIError):
    """Raised on HTTP 429 Too Many Requests."""


class APIAuthError(BackendAPIError):
    """Raised on HTTP 401 Unauthorized or HTTP 403 Forbidden."""


class APIClientError(BackendAPIError):
    """Raised on HTTP 4xx client errors (400, 404, 409, 422, etc.)."""


class BadRequestError(APIClientError):
    """Raised on HTTP 400 Bad Request."""


class NotFoundError(APIClientError):
    """Raised on HTTP 404 Not Found."""


class ConflictError(APIClientError):
    """Raised on HTTP 409 Conflict."""


class ValidationAPIError(APIClientError):
    """Raised on HTTP 422 Unprocessable Content."""


class APIUnavailableError(BackendAPIError):
    """Raised on HTTP 5xx, network errors, timeouts, or non-JSON / malformed responses."""


def _redact_secret(text: str, secret: str) -> str:
    """Scrub ``secret`` from any error text before attaching or logging it."""
    if secret and secret in text:
        return text.replace(secret, "[REDACTED]")
    return text


def _extract_error_detail(response: httpx.Response, *, secret: str) -> str | None:
    """Safely extract FastAPI ``detail`` text from an error response."""
    try:
        payload = response.json()
    except ValueError:
        return None

    if not isinstance(payload, dict) or "detail" not in payload:
        return None

    raw_detail = payload["detail"]
    extracted: str | None = None
    if isinstance(raw_detail, str):
        extracted = raw_detail.strip()
    elif isinstance(raw_detail, list):
        parts: list[str] = []
        for item in raw_detail:
            if isinstance(item, dict) and isinstance(item.get("msg"), str):
                parts.append(item["msg"].strip())
            elif item is not None:
                parts.append(str(item).strip())
        extracted = "; ".join(part for part in parts if part)
    elif raw_detail is not None:
        extracted = str(raw_detail).strip()

    if not extracted:
        return None

    return _redact_secret(extracted, secret)


def _validate_telegram_user_id(telegram_user_id: int) -> int:
    if isinstance(telegram_user_id, bool) or not isinstance(telegram_user_id, int):
        raise ValueError("telegram_user_id must be a positive integer.")
    if telegram_user_id <= 0:
        raise ValueError("telegram_user_id must be a positive integer.")
    return telegram_user_id


class BackendAPIClient:
    """Typed async client communicating with the Backend HTTP API via ``httpx``."""

    def __init__(
        self,
        base_url: str,
        internal_api_key: str,
        *,
        timeout: float = DEFAULT_BACKEND_API_TIMEOUT_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        cleaned_base_url = base_url.strip().rstrip("/")
        if not cleaned_base_url:
            raise ValueError("base_url must be a non-empty URL.")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number.")

        self._base_url = cleaned_base_url
        self._internal_api_key = internal_api_key
        self._timeout = float(timeout)
        self._owns_client = http_client is None
        self._client = (
            http_client
            if http_client is not None
            else httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout),
            )
        )

    @property
    def base_url(self) -> str:
        """Configured backend API base URL without trailing slash."""
        return self._base_url

    @property
    def timeout(self) -> float:
        """Configured request timeout in seconds."""
        return self._timeout

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` if owned by this instance."""
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> BackendAPIClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def _build_headers(self, telegram_user_id: int) -> dict[str, str]:
        validated_user_id = _validate_telegram_user_id(telegram_user_id)
        return {
            "X-Internal-API-Key": self._internal_api_key,
            "X-Telegram-User-Id": str(validated_user_id),
            "Accept": "application/json",
        }

    def _raise_for_error_response(
        self,
        response: httpx.Response,
        *,
        method: str,
        path: str,
        telegram_user_id: int,
    ) -> None:
        status_code = response.status_code
        detail = _extract_error_detail(response, secret=self._internal_api_key)

        logger.warning(
            "Backend API error: method=%s path=%s status=%d user_id=%d",
            method,
            path,
            status_code,
            telegram_user_id,
        )

        if status_code == 429:
            raise RateLimitError(
                RATE_LIMIT_MESSAGE_UA,
                status_code=status_code,
                detail=detail,
            )

        if status_code in (401, 403):
            raise APIAuthError(
                AUTH_ERROR_MESSAGE_UA,
                status_code=status_code,
                detail=None,
            )

        if status_code == 400:
            raise BadRequestError(
                detail or CLIENT_ERROR_FALLBACK_MESSAGE_UA,
                status_code=status_code,
                detail=detail,
            )

        if status_code == 404:
            raise NotFoundError(
                detail or CLIENT_ERROR_FALLBACK_MESSAGE_UA,
                status_code=status_code,
                detail=detail,
            )

        if status_code == 409:
            raise ConflictError(
                detail or CLIENT_ERROR_FALLBACK_MESSAGE_UA,
                status_code=status_code,
                detail=detail,
            )

        if status_code == 422:
            raise ValidationAPIError(
                detail or CLIENT_ERROR_FALLBACK_MESSAGE_UA,
                status_code=status_code,
                detail=detail,
            )

        if 400 <= status_code < 500:
            raise APIClientError(
                detail or CLIENT_ERROR_FALLBACK_MESSAGE_UA,
                status_code=status_code,
                detail=detail,
            )

        raise APIUnavailableError(
            UNAVAILABLE_ERROR_MESSAGE_UA,
            status_code=status_code,
            detail=None,
        )

    @overload
    async def _request(
        self,
        method: str,
        path: str,
        *,
        telegram_user_id: int,
        response_model: type[_ResponseModelT],
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> _ResponseModelT: ...

    @overload
    async def _request(
        self,
        method: str,
        path: str,
        *,
        telegram_user_id: int,
        response_model: None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> None: ...

    async def _request(
        self,
        method: str,
        path: str,
        *,
        telegram_user_id: int,
        response_model: type[_ResponseModelT] | None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> _ResponseModelT | None:
        headers = self._build_headers(telegram_user_id)
        try:
            response = await self._client.request(
                method,
                path,
                headers=headers,
                params=params,
                json=json,
                files=files,
            )
        except httpx.TimeoutException as exc:
            logger.warning(
                "Backend API timeout: method=%s path=%s user_id=%d error=%s",
                method,
                path,
                telegram_user_id,
                type(exc).__name__,
            )
            raise APIUnavailableError(
                TIMEOUT_ERROR_MESSAGE_UA,
                status_code=None,
                detail=None,
            ) from None
        except httpx.RequestError as exc:
            logger.warning(
                "Backend API connection error: method=%s path=%s user_id=%d error=%s",
                method,
                path,
                telegram_user_id,
                type(exc).__name__,
            )
            raise APIUnavailableError(
                UNAVAILABLE_ERROR_MESSAGE_UA,
                status_code=None,
                detail=None,
            ) from None

        if response.status_code < 200 or response.status_code >= 300:
            self._raise_for_error_response(
                response,
                method=method,
                path=path,
                telegram_user_id=telegram_user_id,
            )

        if response_model is None:
            return None

        try:
            payload = response.json()
        except ValueError:
            logger.warning(
                "Backend API returned non-JSON response: method=%s path=%s status=%d user_id=%d",
                method,
                path,
                response.status_code,
                telegram_user_id,
            )
            raise APIUnavailableError(
                UNAVAILABLE_ERROR_MESSAGE_UA,
                status_code=response.status_code,
                detail=None,
            ) from None

        try:
            return response_model.model_validate(payload)
        except ValidationError:
            logger.warning(
                "Backend API payload validation failed: method=%s path=%s status=%d user_id=%d",
                method,
                path,
                response.status_code,
                telegram_user_id,
            )
            raise APIUnavailableError(
                UNAVAILABLE_ERROR_MESSAGE_UA,
                status_code=response.status_code,
                detail=None,
            ) from None

    # --- Users endpoints ---

    async def get_me(self, telegram_user_id: int) -> UserProfile:
        """Fetch or auto-initialize the user's profile via ``GET /api/users/me``."""
        return await self._request(
            "GET",
            "/api/users/me",
            telegram_user_id=telegram_user_id,
            response_model=UserProfile,
        )

    async def update_settings(
        self,
        telegram_user_id: int,
        *,
        level: UserLevel | _UnsetType = _UNSET,
        exercises_per_set: int | _UnsetType = _UNSET,
        theme: UserTheme | _UnsetType = _UNSET,
        sheets_url: str | None | _UnsetType = _UNSET,
        reminder_hour: int | _UnsetType = _UNSET,
        timezone: str | _UnsetType = _UNSET,
    ) -> UserProfile:
        """Partially update user settings via ``PATCH /api/users/settings``."""
        payload: dict[str, Any] = {}
        if not isinstance(level, _UnsetType):
            payload["level"] = level
        if not isinstance(exercises_per_set, _UnsetType):
            payload["exercises_per_set"] = exercises_per_set
        if not isinstance(theme, _UnsetType):
            payload["theme"] = theme
        if not isinstance(sheets_url, _UnsetType):
            payload["sheets_url"] = sheets_url
        if not isinstance(reminder_hour, _UnsetType):
            payload["reminder_hour"] = reminder_hour
        if not isinstance(timezone, _UnsetType):
            payload["timezone"] = timezone

        return await self._request(
            "PATCH",
            "/api/users/settings",
            telegram_user_id=telegram_user_id,
            response_model=UserProfile,
            json=payload,
        )

    # --- Words endpoints ---

    async def list_words(
        self,
        telegram_user_id: int,
        *,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> WordList:
        """Fetch paginated user vocabulary via ``GET /api/words``."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if search is not None:
            params["search"] = search
        return await self._request(
            "GET",
            "/api/words",
            telegram_user_id=telegram_user_id,
            response_model=WordList,
            params=params,
        )

    async def import_words_text(self, telegram_user_id: int, text: str) -> WordImportResult:
        """Import vocabulary words from raw text via ``POST /api/words/import``."""
        return await self._request(
            "POST",
            "/api/words/import",
            telegram_user_id=telegram_user_id,
            response_model=WordImportResult,
            json={"text": text},
        )

    async def import_words_file(
        self,
        telegram_user_id: int,
        filename: str,
        content: bytes,
        *,
        content_type: str = "text/plain",
    ) -> WordImportResult:
        """Import vocabulary words from a ``.txt``/``.csv`` file via ``POST /api/words/import``."""
        return await self._request(
            "POST",
            "/api/words/import",
            telegram_user_id=telegram_user_id,
            response_model=WordImportResult,
            files={"file": (filename, content, content_type)},
        )

    async def sync_sheets(
        self,
        telegram_user_id: int,
        *,
        sheets_url: str | None = None,
    ) -> WordImportResult:
        """Sync vocabulary from Google Sheets via ``POST /api/words/sync-sheets``."""
        payload = {"sheets_url": sheets_url} if sheets_url is not None else None
        return await self._request(
            "POST",
            "/api/words/sync-sheets",
            telegram_user_id=telegram_user_id,
            response_model=WordImportResult,
            json=payload,
        )

    async def delete_word(self, telegram_user_id: int, word_id: int) -> None:
        """Delete a word owned by the user via ``DELETE /api/words/{word_id}``."""
        await self._request(
            "DELETE",
            f"/api/words/{word_id}",
            telegram_user_id=telegram_user_id,
            response_model=None,
        )

    # --- Sessions endpoints ---

    async def start_session(
        self,
        telegram_user_id: int,
        *,
        mode: SessionMode = "mix",
        target_topic: str | None = None,
        total_exercises: int | None = None,
    ) -> SessionStartResponse:
        """Start a new training session via ``POST /api/sessions/start``."""
        payload: dict[str, Any] = {"mode": mode}
        if target_topic is not None:
            payload["target_topic"] = target_topic
        if total_exercises is not None:
            payload["total_exercises"] = total_exercises
        return await self._request(
            "POST",
            "/api/sessions/start",
            telegram_user_id=telegram_user_id,
            response_model=SessionStartResponse,
            json=payload,
        )

    async def get_active_session(self, telegram_user_id: int) -> ActiveSessionResponse:
        """Retrieve the user's currently active session via ``GET /api/sessions/active``."""
        return await self._request(
            "GET",
            "/api/sessions/active",
            telegram_user_id=telegram_user_id,
            response_model=ActiveSessionResponse,
        )

    async def request_hint(
        self,
        telegram_user_id: int,
        session_id: int,
    ) -> SessionHintResponse:
        """Request a hint for the active exercise via ``POST /api/sessions/{id}/hint``."""
        return await self._request(
            "POST",
            f"/api/sessions/{session_id}/hint",
            telegram_user_id=telegram_user_id,
            response_model=SessionHintResponse,
        )

    async def submit_answer(
        self,
        telegram_user_id: int,
        session_id: int,
        user_answer: str,
    ) -> SessionAnswerResponse:
        """Submit an exercise answer via ``POST /api/sessions/{id}/answer``."""
        return await self._request(
            "POST",
            f"/api/sessions/{session_id}/answer",
            telegram_user_id=telegram_user_id,
            response_model=SessionAnswerResponse,
            json={"user_answer": user_answer},
        )

    async def abandon_session(
        self,
        telegram_user_id: int,
        session_id: int,
    ) -> SessionAbandonResponse:
        """Abandon an active training session via ``POST /api/sessions/{id}/abandon``."""
        return await self._request(
            "POST",
            f"/api/sessions/{session_id}/abandon",
            telegram_user_id=telegram_user_id,
            response_model=SessionAbandonResponse,
        )

    # --- Stats endpoints ---

    async def get_radar_stats(self, telegram_user_id: int) -> RadarStatsResponse:
        """Fetch 5-axis B1-calibrated radar stats via ``GET /api/stats/radar``."""
        return await self._request(
            "GET",
            "/api/stats/radar",
            telegram_user_id=telegram_user_id,
            response_model=RadarStatsResponse,
        )


__all__ = [
    "AUTH_ERROR_MESSAGE_UA",
    "CLIENT_ERROR_FALLBACK_MESSAGE_UA",
    "RATE_LIMIT_MESSAGE_UA",
    "TIMEOUT_ERROR_MESSAGE_UA",
    "UNAVAILABLE_ERROR_MESSAGE_UA",
    "APIAuthError",
    "APIClientError",
    "APIUnavailableError",
    "BackendAPIClient",
    "BackendAPIError",
    "BadRequestError",
    "ConflictError",
    "NotFoundError",
    "RateLimitError",
    "ValidationAPIError",
]
