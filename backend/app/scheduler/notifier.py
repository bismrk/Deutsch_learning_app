"""Async Telegram Bot API client for isolated scheduler notifications."""

from __future__ import annotations

import html
import logging
import re
from types import TracebackType
from typing import Any

import httpx

DEFAULT_TELEGRAM_API_BASE_URL = "https://api.telegram.org"
DEFAULT_TELEGRAM_TIMEOUT_SECONDS = 10.0
MAX_TELEGRAM_TIMEOUT_SECONDS = 10.0

_BOT_URL_TOKEN_PATTERN = re.compile(r"/bot[^/\s\"']+")


def escape_telegram_html(text: str) -> str:
    """Escape `<`, `>`, and `&` for safe Telegram Bot API HTML rendering."""
    return html.escape(text, quote=False)


def redact_bot_token(text: str, bot_token: str | None = None) -> str:
    """Remove any occurrence of a Telegram bot token or `/bot<token>` URL segment."""
    sanitized = _BOT_URL_TOKEN_PATTERN.sub("/bot[REDACTED]", text)
    if bot_token:
        stripped = bot_token.strip()
        if stripped:
            sanitized = sanitized.replace(stripped, "[REDACTED]")
    return sanitized


class TokenRedactionFilter(logging.Filter):
    """Logging filter preventing Telegram bot token leakage in log records."""

    def __init__(self, bot_token: str | None = None) -> None:
        super().__init__()
        self._bot_token = bot_token.strip() if bot_token and bot_token.strip() else None

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        sanitized = redact_bot_token(rendered, self._bot_token)
        if sanitized != rendered:
            record.msg = sanitized
            record.args = None
        return True


class TelegramNotifierError(RuntimeError):
    """Base error for Telegram scheduler notification failures."""


class TelegramConfigError(TelegramNotifierError):
    """Raised when Telegram notifier configuration (such as BOT_TOKEN) is missing or invalid."""


class TelegramDeliveryError(TelegramNotifierError):
    """Raised when sending a Telegram message fails, without exposing BOT_TOKEN."""

    def __init__(
        self,
        message: str,
        *,
        chat_id: int,
        status_code: int | None,
        reason: str,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(redact_bot_token(message))
        self.chat_id = chat_id
        self.status_code = status_code
        self.reason = reason
        self.retry_after = retry_after


def _extract_retry_after(body: Any, headers: httpx.Headers) -> int | None:
    if isinstance(body, dict):
        parameters = body.get("parameters")
        if isinstance(parameters, dict):
            raw = parameters.get("retry_after")
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                return raw

    header_value = headers.get("Retry-After")
    if header_value and header_value.strip().isdigit():
        return int(header_value.strip())
    return None


def _classify_http_status(status_code: int) -> str:
    if status_code == 403:
        return "forbidden"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "server_error"
    return "client_error"


class TelegramNotifier:
    """Direct async HTTP client for Telegram Bot API `sendMessage`."""

    def __init__(
        self,
        bot_token: str | None,
        *,
        base_url: str = DEFAULT_TELEGRAM_API_BASE_URL,
        timeout_seconds: float = DEFAULT_TELEGRAM_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if bot_token is None or not bot_token.strip():
            raise TelegramConfigError("BOT_TOKEN is not configured")

        if timeout_seconds <= 0 or timeout_seconds > MAX_TELEGRAM_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must be in (0, {MAX_TELEGRAM_TIMEOUT_SECONDS}]",
            )

        clean_base_url = base_url.strip().rstrip("/")
        if not clean_base_url:
            raise ValueError("base_url must not be empty")

        self._bot_token = bot_token.strip()
        self._base_url = clean_base_url
        self._timeout_seconds = float(timeout_seconds)
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_seconds))
        )

    def __repr__(self) -> str:
        return (
            f"TelegramNotifier(base_url={self._base_url!r}, "
            f"timeout_seconds={self._timeout_seconds!r})"
        )

    @property
    def timeout_seconds(self) -> float:
        """Configured per-request timeout in seconds."""
        return self._timeout_seconds

    @property
    def _send_message_url(self) -> str:
        return f"{self._base_url}/bot{self._bot_token}/sendMessage"

    async def __aenter__(self) -> TelegramNotifier:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying `httpx.AsyncClient` when owned by this notifier."""
        if self._owns_client:
            await self._client.aclose()

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = "HTML",
        is_html: bool = False,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        """Send a single Telegram message via Bot API without leaking `BOT_TOKEN`."""
        prepared_text = (
            escape_telegram_html(text) if parse_mode == "HTML" and not is_html else text
        )
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": prepared_text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode

        try:
            response = await self._client.post(
                self._send_message_url,
                json=payload,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException:
            raise TelegramDeliveryError(
                f"Telegram API request timed out for chat_id={chat_id}",
                chat_id=chat_id,
                status_code=None,
                reason="timeout",
            ) from None
        except httpx.RequestError:
            raise TelegramDeliveryError(
                f"Telegram API network error for chat_id={chat_id}",
                chat_id=chat_id,
                status_code=None,
                reason="network_error",
            ) from None

        try:
            body: Any = response.json()
        except ValueError:
            body = None

        description = (
            redact_bot_token(str(body["description"]), self._bot_token)
            if isinstance(body, dict) and body.get("description") is not None
            else None
        )

        if response.status_code >= 400:
            reason = _classify_http_status(response.status_code)
            retry_after = _extract_retry_after(body, response.headers)
            detail = f": {description}" if description else ""
            raise TelegramDeliveryError(
                f"Telegram API returned HTTP {response.status_code} ({reason}) "
                f"for chat_id={chat_id}{detail}",
                chat_id=chat_id,
                status_code=response.status_code,
                reason=reason,
                retry_after=retry_after,
            ) from None

        if not isinstance(body, dict) or body.get("ok") is not True:
            detail = f": {description}" if description else ""
            raise TelegramDeliveryError(
                f"Telegram API rejected message for chat_id={chat_id}{detail}",
                chat_id=chat_id,
                status_code=response.status_code,
                reason="api_error",
            ) from None

        return body


__all__ = [
    "DEFAULT_TELEGRAM_API_BASE_URL",
    "DEFAULT_TELEGRAM_TIMEOUT_SECONDS",
    "MAX_TELEGRAM_TIMEOUT_SECONDS",
    "TelegramConfigError",
    "TelegramDeliveryError",
    "TelegramNotifier",
    "TelegramNotifierError",
    "TokenRedactionFilter",
    "escape_telegram_html",
    "redact_bot_token",
]
