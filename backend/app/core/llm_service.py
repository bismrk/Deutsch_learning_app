"""Provider-agnostic async LLM client over OpenAI SDK with tenacity retries and Pydantic v2."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import TracebackType
from typing import Any, Self, TypeVar

import httpx
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    OpenAIError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)

DEFAULT_LLM_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_PARSE_RETRIES = 1
DEFAULT_TRANSPORT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_MULTIPLIER = 1.0
DEFAULT_BACKOFF_MIN_SECONDS = 1.0
DEFAULT_BACKOFF_MAX_SECONDS = 8.0

TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 429})
AUTH_HTTP_STATUS_CODES = frozenset({401, 403})

_UNSET: Any = object()


class LLMError(Exception):
    """Base exception for all LLM service failures."""


class LLMConfigurationError(LLMError):
    """Raised when required LLM configuration (such as LLM_API_KEY) is missing or invalid."""


class LLMAuthenticationError(LLMError):
    """Raised on non-retriable authentication/authorization failures (HTTP 401/403)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class LLMRequestError(LLMError):
    """Raised on non-retriable client/provider request errors (e.g., HTTP 400/404/422)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class LLMUnavailableError(LLMError):
    """Raised when transient provider/network errors persist after tenacity retries."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class LLMInvalidResponseError(LLMError):
    """Raised when LLM output is empty, invalid JSON, or fails Pydantic validation after retry."""


class _EmptyLLMContentError(Exception):
    """Internal marker raised when a completion response contains no usable message text."""


def _validate_positive_finite_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a positive finite number.")
    as_float = float(value)
    if not math.isfinite(as_float) or as_float <= 0.0:
        raise ValueError(f"{field_name} must be a positive finite number.")
    return as_float


def _validate_non_negative_finite_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a non-negative finite number.")
    as_float = float(value)
    if not math.isfinite(as_float) or as_float < 0.0:
        raise ValueError(f"{field_name} must be a non-negative finite number.")
    return as_float


def _validate_int_range(value: object, *, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}.")
    return int(value)


def _is_transient_provider_error(exc: BaseException) -> bool:
    """Return True only for transient errors eligible for tenacity exponential backoff."""
    if isinstance(
        exc,
        (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError),
    ):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in TRANSIENT_HTTP_STATUS_CODES or exc.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


def _translate_provider_error(
    exc: BaseException,
    *,
    attempts_made: int,
) -> LLMError:
    """Map SDK/HTTP errors into typed LLMError subclasses without leaking credentials."""
    if isinstance(exc, (APITimeoutError, httpx.TimeoutException)):
        return LLMUnavailableError(
            f"LLM provider request timed out after {attempts_made} attempt(s).",
        )
    if isinstance(exc, (APIConnectionError, httpx.NetworkError)):
        return LLMUnavailableError(
            f"LLM provider connection error after {attempts_made} attempt(s).",
        )
    if isinstance(exc, APIStatusError):
        status_code = exc.status_code
        if status_code in TRANSIENT_HTTP_STATUS_CODES or status_code >= 500:
            return LLMUnavailableError(
                f"LLM provider temporarily unavailable (HTTP {status_code}) "
                f"after {attempts_made} attempt(s).",
                status_code=status_code,
            )
        if status_code in AUTH_HTTP_STATUS_CODES:
            return LLMAuthenticationError(
                f"LLM provider authentication failed (HTTP {status_code}).",
                status_code=status_code,
            )
        return LLMRequestError(
            f"LLM provider rejected the request (HTTP {status_code}).",
            status_code=status_code,
        )
    return LLMRequestError(
        f"LLM provider client error ({type(exc).__name__}).",
    )


def _normalize_messages(
    *,
    messages: Sequence[Mapping[str, str]] | None,
    system_prompt: str | None,
    user_prompt: str | None,
) -> list[dict[str, str]]:
    """Build and validate Chat Completions messages from either explicit messages or prompts."""
    if messages is not None and (system_prompt is not None or user_prompt is not None):
        raise ValueError("Pass either 'messages' or 'system_prompt'/'user_prompt', not both.")

    normalized: list[dict[str, str]] = []
    if messages is not None:
        for item in messages:
            if not isinstance(item, Mapping):
                raise ValueError("Each message must be a mapping with 'role' and 'content'.")
            role = item.get("role")
            content = item.get("content")
            if not isinstance(role, str) or not role.strip():
                raise ValueError("Each message must include a non-empty string 'role'.")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Each message must include a non-empty string 'content'.")
            normalized.append({"role": role.strip(), "content": content})
    else:
        if system_prompt is not None:
            if not isinstance(system_prompt, str) or not system_prompt.strip():
                raise ValueError("system_prompt must be a non-empty string.")
            normalized.append({"role": "system", "content": system_prompt})
        if user_prompt is not None:
            if not isinstance(user_prompt, str) or not user_prompt.strip():
                raise ValueError("user_prompt must be a non-empty string.")
            normalized.append({"role": "user", "content": user_prompt})

    if not normalized:
        raise ValueError("At least one message or prompt must be provided.")
    return normalized


def _extract_message_content(completion: Any) -> str:
    """Extract non-empty text content from the first ChatCompletion choice."""
    choices = getattr(completion, "choices", None)
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise _EmptyLLMContentError("LLM completion returned no choices.")
    first_choice = choices[0]
    message = getattr(first_choice, "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise _EmptyLLMContentError("LLM completion returned empty message content.")
    return content.strip()


class LLMService:
    """Provider-agnostic async LLM service built on AsyncOpenAI, tenacity, and Pydantic v2.

    Configuration is supplied exclusively through ``Settings`` (``LLM_BASE_URL``,
    ``LLM_API_KEY``, ``LLM_MODEL``, ``LLM_TIMEOUT_SECONDS``) or explicit constructor
    overrides without hardcoding any provider URL. SDK-level retries are disabled
    (``max_retries=0``) so transient transport retries are governed solely by ``tenacity``.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_url: str | None = _UNSET,
        api_key: str | None = _UNSET,
        model: str | None = _UNSET,
        timeout: float | None = _UNSET,
        http_client: httpx.AsyncClient | None = None,
        max_parse_retries: int = DEFAULT_MAX_PARSE_RETRIES,
        transport_max_attempts: int = DEFAULT_TRANSPORT_MAX_ATTEMPTS,
        backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
        backoff_min_seconds: float = DEFAULT_BACKOFF_MIN_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        resolved_settings = settings if settings is not None else get_settings()

        raw_base_url = resolved_settings.llm_base_url if base_url is _UNSET else base_url
        raw_api_key = resolved_settings.llm_api_key if api_key is _UNSET else api_key
        raw_model = resolved_settings.llm_model if model is _UNSET else model
        raw_timeout = (
            getattr(resolved_settings, "llm_timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS)
            if timeout is _UNSET
            else timeout
        )

        self._base_url = raw_base_url.strip() if isinstance(raw_base_url, str) else ""
        self._api_key = raw_api_key.strip() if isinstance(raw_api_key, str) else ""
        self._model = raw_model.strip() if isinstance(raw_model, str) else ""
        self._timeout = _validate_positive_finite_float(raw_timeout, field_name="timeout")
        self._max_parse_retries = _validate_int_range(
            max_parse_retries,
            field_name="max_parse_retries",
            minimum=0,
        )
        self._transport_max_attempts = _validate_int_range(
            transport_max_attempts,
            field_name="transport_max_attempts",
            minimum=1,
        )
        self._backoff_multiplier = _validate_non_negative_finite_float(
            backoff_multiplier,
            field_name="backoff_multiplier",
        )
        self._backoff_min_seconds = _validate_non_negative_finite_float(
            backoff_min_seconds,
            field_name="backoff_min_seconds",
        )
        self._backoff_max_seconds = _validate_non_negative_finite_float(
            backoff_max_seconds,
            field_name="backoff_max_seconds",
        )
        if self._backoff_max_seconds < self._backoff_min_seconds:
            raise ValueError("backoff_max_seconds must be >= backoff_min_seconds.")

        self._sleep = sleep
        self._owns_http_client = http_client is None
        self._client: AsyncOpenAI | None = None

        if self._api_key and self._base_url and self._model:
            client_kwargs: dict[str, Any] = {
                "base_url": self._base_url,
                "api_key": self._api_key,
                "timeout": self._timeout,
                "max_retries": 0,
            }
            if http_client is not None:
                client_kwargs["http_client"] = http_client
            self._client = AsyncOpenAI(**client_kwargs)

    @property
    def base_url(self) -> str:
        """Configured OpenAI-compatible base URL."""
        return self._base_url

    @property
    def model(self) -> str:
        """Configured chat completion model name."""
        return self._model

    @property
    def timeout(self) -> float:
        """Configured per-request HTTP timeout in seconds."""
        return self._timeout

    @property
    def client(self) -> AsyncOpenAI | None:
        """Underlying AsyncOpenAI client (None when credentials/config are incomplete)."""
        return self._client

    def __repr__(self) -> str:
        return (
            f"LLMService(base_url={self._base_url!r}, "
            f"model={self._model!r}, timeout={self._timeout!r})"
        )

    async def aclose(self) -> None:
        """Close the internally managed AsyncOpenAI HTTP client if present."""
        if self._client is not None and self._owns_http_client:
            await self._client.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _require_configured_client(self) -> AsyncOpenAI:
        if not self._api_key:
            raise LLMConfigurationError("LLM_API_KEY is not configured.")
        if not self._base_url:
            raise LLMConfigurationError("LLM_BASE_URL is not configured.")
        if not self._model:
            raise LLMConfigurationError("LLM_MODEL is not configured.")
        if self._client is None:
            raise LLMConfigurationError("LLM client is not initialized.")
        return self._client

    async def _request_completion(
        self,
        client: AsyncOpenAI,
        *,
        messages: list[dict[str, str]],
        temperature: float | None,
        max_tokens: int | None,
    ) -> Any:
        retrying_kwargs: dict[str, Any] = {
            "retry": retry_if_exception(_is_transient_provider_error),
            "stop": stop_after_attempt(self._transport_max_attempts),
            "wait": wait_exponential(
                multiplier=self._backoff_multiplier,
                min=self._backoff_min_seconds,
                max=self._backoff_max_seconds,
            ),
            "reraise": True,
        }
        if self._sleep is not None:
            retrying_kwargs["sleep"] = self._sleep

        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if temperature is not None:
            request_kwargs["temperature"] = temperature
        if max_tokens is not None:
            request_kwargs["max_tokens"] = max_tokens

        attempts_made = 0
        try:
            async for attempt in AsyncRetrying(**retrying_kwargs):
                with attempt:
                    attempts_made += 1
                    return await client.chat.completions.create(**request_kwargs)
        except (json.JSONDecodeError, APIResponseValidationError):
            raise
        except (
            APIStatusError,
            APITimeoutError,
            APIConnectionError,
            httpx.TimeoutException,
            httpx.NetworkError,
            OpenAIError,
        ) as exc:
            logger.warning(
                "LLM provider request failed after %d attempt(s): %s (status=%s)",
                attempts_made,
                type(exc).__name__,
                getattr(exc, "status_code", None),
            )
            raise _translate_provider_error(exc, attempts_made=attempts_made) from None

    async def generate_structured(
        self,
        *,
        response_model: type[ResponseModelT],
        messages: Sequence[Mapping[str, str]] | None = None,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ResponseModelT:
        """Send a JSON-mode chat completion request and validate output via Pydantic v2.

        Always passes ``response_format={"type": "json_object"}`` and the configured
        ``LLM_MODEL``. Retries transient transport/provider failures with ``tenacity``
        exponential backoff. On empty content, invalid JSON, or Pydantic
        ``ValidationError``, performs up to ``max_parse_retries`` (default ``1``)
        automatic retry before raising ``LLMInvalidResponseError``.
        """
        if not (isinstance(response_model, type) and issubclass(response_model, BaseModel)):
            raise TypeError("response_model must be a Pydantic BaseModel subclass.")

        if temperature is not None:
            temperature = _validate_non_negative_finite_float(
                temperature,
                field_name="temperature",
            )
        if max_tokens is not None:
            max_tokens = _validate_int_range(max_tokens, field_name="max_tokens", minimum=1)

        normalized_messages = _normalize_messages(
            messages=messages,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        client = self._require_configured_client()

        total_attempts = 1 + self._max_parse_retries
        for attempt_index in range(total_attempts):
            try:
                completion = await self._request_completion(
                    client,
                    messages=normalized_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                raw_content = _extract_message_content(completion)
                return response_model.model_validate_json(raw_content)
            except (
                _EmptyLLMContentError,
                ValidationError,
                json.JSONDecodeError,
                APIResponseValidationError,
            ) as exc:
                failure_kind = type(exc).__name__
                if attempt_index + 1 < total_attempts:
                    logger.warning(
                        "LLM structured response invalid (%s) on attempt %d/%d; retrying once.",
                        failure_kind,
                        attempt_index + 1,
                        total_attempts,
                    )
                    continue
                raise LLMInvalidResponseError(
                    f"LLM returned invalid structured JSON response after "
                    f"{total_attempts} attempt(s) ({failure_kind}).",
                ) from None

        raise AssertionError("Unreachable structured generation state.")  # pragma: no cover


__all__ = [
    "AUTH_HTTP_STATUS_CODES",
    "DEFAULT_BACKOFF_MAX_SECONDS",
    "DEFAULT_BACKOFF_MIN_SECONDS",
    "DEFAULT_BACKOFF_MULTIPLIER",
    "DEFAULT_LLM_TIMEOUT_SECONDS",
    "DEFAULT_MAX_PARSE_RETRIES",
    "DEFAULT_TRANSPORT_MAX_ATTEMPTS",
    "LLMAuthenticationError",
    "LLMConfigurationError",
    "LLMError",
    "LLMInvalidResponseError",
    "LLMRequestError",
    "LLMService",
    "LLMUnavailableError",
    "TRANSIENT_HTTP_STATUS_CODES",
]
