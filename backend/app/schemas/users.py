"""Pydantic schemas and strict validators for user profile and settings endpoints."""

from datetime import datetime
from typing import Any, Literal
from urllib.parse import unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

UserLevel = Literal["A1", "A2", "B1"]
UserTheme = Literal["system", "dark", "light"]

ALLOWED_SHEETS_HOST = "docs.google.com"
ALLOWED_SHEETS_PATH_PREFIX = "/spreadsheets/"
MAX_SHEETS_URL_LENGTH = 2048
MAX_TIMEZONE_LENGTH = 64

_AVAILABLE_IANA_TIMEZONES: frozenset[str] = frozenset(available_timezones())


def validate_sheets_url(value: Any) -> str | None:
    """Validate and normalize a Google Sheets HTTPS URL without network requests.

    ``None`` and empty/whitespace-only strings clear the field to ``None``.
    Non-empty URLs must target ``https://docs.google.com/spreadsheets/...`` with no
    embedded credentials, non-HTTPS ports, control/whitespace characters, backslashes,
    or path traversal segments.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("sheets_url must be a string or null")

    trimmed = value.strip()
    if not trimmed:
        return None

    if len(trimmed) > MAX_SHEETS_URL_LENGTH:
        raise ValueError("sheets_url is too long")

    if "\\" in trimmed or any(ch.isspace() or ord(ch) < 32 for ch in trimmed):
        raise ValueError("sheets_url contains invalid characters")

    try:
        parsed = urlsplit(trimmed)
    except ValueError as exc:
        raise ValueError("sheets_url is not a valid URL") from exc

    if parsed.scheme.lower() != "https":
        raise ValueError("sheets_url must use the https scheme")

    if not trimmed[len(parsed.scheme) :].startswith("://"):
        raise ValueError("sheets_url must be an absolute HTTPS URL")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("sheets_url must not contain credentials")

    hostname = (parsed.hostname or "").lower()
    if hostname != ALLOWED_SHEETS_HOST:
        raise ValueError("sheets_url must point to docs.google.com")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("sheets_url has an invalid port") from exc

    if port is not None and port != 443:
        raise ValueError("sheets_url must not specify a non-standard port")

    if not parsed.path.startswith(ALLOWED_SHEETS_PATH_PREFIX):
        raise ValueError("sheets_url path must start with /spreadsheets/")

    relative_path = parsed.path[len(ALLOWED_SHEETS_PATH_PREFIX) :]
    if not relative_path or not relative_path.strip("/"):
        raise ValueError("sheets_url must reference a spreadsheet path")

    for segment in parsed.path.split("/"):
        decoded = unquote(segment)
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded:
            raise ValueError("sheets_url contains invalid path segments")

    return urlunsplit(("https", ALLOWED_SHEETS_HOST, parsed.path, parsed.query, parsed.fragment))


def validate_iana_timezone(value: Any) -> str:
    """Validate that a timezone string is an IANA zone loadable via ``zoneinfo.ZoneInfo``."""
    if not isinstance(value, str):
        raise ValueError("timezone must be a string")

    if (
        not value
        or len(value) > MAX_TIMEZONE_LENGTH
        or value != value.strip()
        or any(ch.isspace() for ch in value)
    ):
        raise ValueError("timezone must be a valid IANA timezone name")

    if value not in _AVAILABLE_IANA_TIMEZONES:
        raise ValueError("timezone must be a valid IANA timezone name")

    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError("timezone must be a valid IANA timezone name") from exc

    return value


class UserProfileResponse(BaseModel):
    """Serialized user profile returned by ``/api/users/me`` and ``/api/users/settings``."""

    model_config = ConfigDict(from_attributes=True)

    telegram_id: int
    level: UserLevel
    exercises_per_set: int
    theme: UserTheme
    sheets_url: str | None
    reminder_hour: int
    timezone: str
    daily_llm_calls: int
    created_at: datetime


class UserSettingsUpdate(BaseModel):
    """Partial update payload for ``PATCH /api/users/settings``."""

    model_config = ConfigDict(extra="forbid")

    level: UserLevel = "A1"
    exercises_per_set: StrictInt = Field(default=5, ge=1, le=30)
    theme: UserTheme = "system"
    sheets_url: str | None = None
    reminder_hour: StrictInt = Field(default=19, ge=0, le=23)
    timezone: StrictStr = "Europe/Kyiv"

    @field_validator("sheets_url", mode="before")
    @classmethod
    def _validate_sheets_url(cls, value: Any) -> str | None:
        return validate_sheets_url(value)

    @field_validator("timezone", mode="after")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        return validate_iana_timezone(value)


__all__ = [
    "ALLOWED_SHEETS_HOST",
    "ALLOWED_SHEETS_PATH_PREFIX",
    "UserLevel",
    "UserProfileResponse",
    "UserSettingsUpdate",
    "UserTheme",
    "validate_iana_timezone",
    "validate_sheets_url",
]
