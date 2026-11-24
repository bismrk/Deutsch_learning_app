"""Authentication helpers for Telegram Mini App initData and internal bot requests."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from urllib.parse import parse_qsl

MIN_TELEGRAM_USER_ID = 1
MAX_TELEGRAM_USER_ID = (1 << 63) - 1
DEFAULT_INIT_DATA_MAX_AGE_SECONDS = 86_400
DEFAULT_INIT_DATA_MAX_FUTURE_SKEW_SECONDS = 300
TELEGRAM_WEBAPP_HMAC_KEY = b"WebAppData"

_MAX_USER_ID_DIGITS = len(str(MAX_TELEGRAM_USER_ID))
_MAX_AUTH_DATE_DIGITS = 15


class AuthError(Exception):
    """Raised when client authentication credentials are missing, malformed, or invalid."""


def parse_telegram_user_id(raw_user_id: str | None) -> int:
    """Parse and validate a positive 64-bit signed BIGINT Telegram user ID."""
    if raw_user_id is None:
        raise AuthError("Missing Telegram user ID")

    normalized = raw_user_id.strip()
    if (
        not normalized
        or len(normalized) > _MAX_USER_ID_DIGITS
        or normalized.startswith("0")
        or not (normalized.isascii() and normalized.isdigit())
    ):
        raise AuthError("Invalid Telegram user ID")

    user_id = int(normalized)
    if not (MIN_TELEGRAM_USER_ID <= user_id <= MAX_TELEGRAM_USER_ID):
        raise AuthError("Telegram user ID out of range")

    return user_id


def extract_tma_init_data(authorization: str) -> str:
    """Extract raw Telegram initData payload from an ``Authorization: tma <initData>`` header."""
    raw = authorization.strip()
    scheme, separator, init_data = raw.partition(" ")
    if not separator or scheme.lower() != "tma":
        raise AuthError("Invalid Authorization scheme")

    payload = init_data.strip()
    if not payload:
        raise AuthError("Missing Telegram initData payload")

    return payload


def _parse_init_data_params(init_data: str) -> dict[str, str]:
    if not init_data or not init_data.strip():
        raise AuthError("Missing Telegram initData payload")

    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=64,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise AuthError("Malformed Telegram initData query string") from exc

    params: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in params:
            raise AuthError("Malformed Telegram initData parameters")
        params[key] = value

    return params


def _parse_auth_date(raw_auth_date: str | None) -> int:
    if raw_auth_date is None:
        raise AuthError("Missing auth_date in Telegram initData")

    if (
        not raw_auth_date
        or len(raw_auth_date) > _MAX_AUTH_DATE_DIGITS
        or raw_auth_date.startswith("0")
        or not (raw_auth_date.isascii() and raw_auth_date.isdigit())
    ):
        raise AuthError("Invalid auth_date in Telegram initData")

    auth_date = int(raw_auth_date)
    if auth_date <= 0:
        raise AuthError("Invalid auth_date in Telegram initData")

    return auth_date


def _parse_user_id_from_json(raw_user: str | None) -> int:
    if raw_user is None:
        raise AuthError("Missing user in Telegram initData")

    try:
        user_payload = json.loads(raw_user)
    except json.JSONDecodeError as exc:
        raise AuthError("Malformed user JSON in Telegram initData") from exc

    if not isinstance(user_payload, dict):
        raise AuthError("Invalid user payload in Telegram initData")

    user_id = user_payload.get("id")
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or not (MIN_TELEGRAM_USER_ID <= user_id <= MAX_TELEGRAM_USER_ID)
    ):
        raise AuthError("Invalid Telegram user ID")

    return user_id


def verify_telegram_init_data(
    init_data: str,
    *,
    bot_token: str | None,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_INIT_DATA_MAX_AGE_SECONDS,
    max_future_skew_seconds: int = DEFAULT_INIT_DATA_MAX_FUTURE_SKEW_SECONDS,
) -> int:
    """Verify Telegram WebApp ``initData`` HMAC-SHA256 signature and freshness.

    Returns the verified positive 64-bit ``user.id`` on success or raises ``AuthError``.
    """
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    if max_future_skew_seconds < 0:
        raise ValueError("max_future_skew_seconds must be non-negative")

    if now is None:
        now_utc = datetime.now(UTC)
    else:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be a timezone-aware datetime")
        now_utc = now.astimezone(UTC)

    if bot_token is None or not bot_token.strip():
        raise AuthError("Telegram bot token is not configured")

    params = _parse_init_data_params(init_data)

    received_hash = params.pop("hash", None)
    if not received_hash:
        raise AuthError("Missing hash in Telegram initData")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
    secret_key = hmac.new(
        TELEGRAM_WEBAPP_HMAC_KEY,
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(received_hash.encode("utf-8"), expected_hash.encode("utf-8")):
        raise AuthError("Invalid Telegram initData signature")

    auth_date = _parse_auth_date(params.get("auth_date"))
    age_seconds = now_utc.timestamp() - auth_date
    if age_seconds > max_age_seconds:
        raise AuthError("Telegram initData has expired")
    if age_seconds < -max_future_skew_seconds:
        raise AuthError("Telegram initData auth_date is too far in the future")

    return _parse_user_id_from_json(params.get("user"))


def verify_internal_bot_auth(
    *,
    api_key: str | None,
    telegram_user_id: str | None,
    expected_api_key: str | None,
) -> int:
    """Verify trusted internal Telegram bot headers and return ``telegram_id``."""
    if expected_api_key is None or not expected_api_key.strip():
        raise AuthError("Internal API key is not configured")

    if api_key is None or not api_key.strip():
        raise AuthError("Missing internal API key")

    if not hmac.compare_digest(api_key.encode("utf-8"), expected_api_key.encode("utf-8")):
        raise AuthError("Invalid internal API key")

    return parse_telegram_user_id(telegram_user_id)


__all__ = [
    "AuthError",
    "DEFAULT_INIT_DATA_MAX_AGE_SECONDS",
    "DEFAULT_INIT_DATA_MAX_FUTURE_SKEW_SECONDS",
    "MAX_TELEGRAM_USER_ID",
    "MIN_TELEGRAM_USER_ID",
    "TELEGRAM_WEBAPP_HMAC_KEY",
    "extract_tma_init_data",
    "parse_telegram_user_id",
    "verify_internal_bot_auth",
    "verify_telegram_init_data",
]
