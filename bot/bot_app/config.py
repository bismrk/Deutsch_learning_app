"""Configuration settings for the standalone Telegram bot service."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["development", "staging", "production", "test"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEFAULT_BACKEND_API_URL = "http://backend:8000"
DEFAULT_BACKEND_API_TIMEOUT_SECONDS = 35.0
MIN_BACKEND_API_TIMEOUT_SECONDS = 30.0


def normalize_https_miniapp_url(url: str | None) -> str | None:
    """Return a validated ``https://`` Mini App URL, or ``None`` if unset or non-HTTPS.

    Telegram Bot API rejects ``WebAppInfo`` URLs that do not use ``https://``.
    Returning ``None`` for empty, HTTP, or malformed local URLs ensures ``/start``
    and main-menu builders safely omit the Mini App button instead of failing.
    """
    if url is None:
        return None

    cleaned = url.strip()
    if not cleaned:
        return None

    if any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
        return None

    if not cleaned.lower().startswith("https://"):
        return None

    try:
        parsed = urlsplit(cleaned)
        _ = parsed.port
    except ValueError:
        return None

    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None

    return f"https://{cleaned[len('https://') :]}"


class BotSettings(BaseSettings):
    """Bot configuration loaded from environment variables and optional ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: AppEnv = "development"
    log_level: LogLevel = "INFO"

    bot_token: str = Field(default="", repr=False)
    internal_api_key: str = Field(default="", repr=False)
    backend_api_url: str = Field(default=DEFAULT_BACKEND_API_URL, min_length=1)
    backend_api_timeout_seconds: float = Field(
        default=DEFAULT_BACKEND_API_TIMEOUT_SECONDS,
        ge=MIN_BACKEND_API_TIMEOUT_SECONDS,
        allow_inf_nan=False,
    )
    miniapp_url: str | None = None

    @field_validator("app_env", mode="before")
    @classmethod
    def _normalize_app_env(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("bot_token", "internal_api_key", mode="before")
    @classmethod
    def _strip_secret_strings(cls, value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("backend_api_url", mode="before")
    @classmethod
    def _normalize_backend_api_url(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().rstrip("/")
        return value

    @field_validator("miniapp_url", mode="before")
    @classmethod
    def _normalize_miniapp_url(cls, value: Any) -> Any:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return value

    @property
    def https_miniapp_url(self) -> str | None:
        """Return ``miniapp_url`` only when it is a valid ``https://`` URL."""
        return normalize_https_miniapp_url(self.miniapp_url)


@lru_cache
def get_settings() -> BotSettings:
    """Return a cached ``BotSettings`` instance."""
    return BotSettings()


__all__ = [
    "AppEnv",
    "BotSettings",
    "DEFAULT_BACKEND_API_TIMEOUT_SECONDS",
    "DEFAULT_BACKEND_API_URL",
    "LogLevel",
    "MIN_BACKEND_API_TIMEOUT_SECONDS",
    "get_settings",
    "normalize_https_miniapp_url",
]
