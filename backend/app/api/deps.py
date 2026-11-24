"""FastAPI authentication dependencies for protected API routes."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.core.auth import (
    AuthError,
    extract_tma_init_data,
    verify_internal_bot_auth,
    verify_telegram_init_data,
)


def get_utc_now() -> datetime:
    """Return the current UTC datetime (overridable in tests for deterministic checks)."""
    return datetime.now(UTC)


def _resolve_active_settings(request: Request, injected_settings: Settings) -> Settings:
    """Resolve active Settings, supporting both ``dependency_overrides[get_settings]``
    and explicit ``create_app(settings=...)`` configuration.
    """
    if get_settings in request.app.dependency_overrides:
        return injected_settings
    app_settings = getattr(request.app.state, "settings", None)
    if isinstance(app_settings, Settings):
        return app_settings
    return injected_settings


def get_current_user_id(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    now: Annotated[datetime, Depends(get_utc_now)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_internal_api_key: Annotated[str | None, Header(alias="X-Internal-API-Key")] = None,
    x_telegram_user_id: Annotated[str | None, Header(alias="X-Telegram-User-Id")] = None,
) -> int:
    """Authenticate Mini App (``Authorization: tma <initData>``) or Telegram bot headers.

    Returns the verified positive 64-bit ``telegram_id`` without accessing the database.
    Raises HTTP 401 on any missing, invalid, forged, or expired credentials without
    leaking server secrets or silently falling back across auth mechanisms.
    """
    resolved_settings = _resolve_active_settings(request, settings)

    try:
        if authorization is not None:
            init_data = extract_tma_init_data(authorization)
            return verify_telegram_init_data(
                init_data,
                bot_token=resolved_settings.bot_token,
                now=now,
                max_age_seconds=resolved_settings.init_data_max_age_seconds,
            )

        if x_internal_api_key is not None or x_telegram_user_id is not None:
            return verify_internal_bot_auth(
                api_key=x_internal_api_key,
                telegram_user_id=x_telegram_user_id,
                expected_api_key=resolved_settings.internal_api_key,
            )

        raise AuthError("Missing authentication credentials")
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing authentication credentials",
            headers={"WWW-Authenticate": "tma"},
        ) from exc


CurrentUserId = Annotated[int, Depends(get_current_user_id)]

__all__ = [
    "CurrentUserId",
    "get_current_user_id",
    "get_utc_now",
]
