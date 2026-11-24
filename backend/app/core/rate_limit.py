"""Atomic per-user daily LLM API quota enforcement backed by PostgreSQL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserId, get_utc_now
from app.config import Settings, get_settings
from app.core.auth import MAX_TELEGRAM_USER_ID, MIN_TELEGRAM_USER_ID
from app.db.base import get_db
from app.db.models import User
from app.db.users import ensure_user_initialized

DEFAULT_RATE_LIMIT_DETAIL_UA = (
    "Денний ліміт запитів до AI-тренера вичерпано. Спробуйте знову завтра."
)


class DailyLLMLimitExceededError(HTTPException):
    """HTTP 429 error raised when a user reaches or exceeds their daily LLM API quota."""

    def __init__(
        self,
        *,
        user_id: int,
        daily_llm_calls: int,
        daily_llm_limit: int,
        reset_date: date,
        detail: str = DEFAULT_RATE_LIMIT_DETAIL_UA,
    ) -> None:
        self.user_id = user_id
        self.daily_llm_calls = daily_llm_calls
        self.daily_llm_limit = daily_llm_limit
        self.reset_date = reset_date
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
        )


@dataclass(frozen=True, slots=True)
class LLMQuotaStatus:
    """Snapshot of a user's daily LLM quota after reserving one API call."""

    user_id: int
    daily_llm_calls: int
    daily_llm_limit: int
    remaining: int
    reset_date: date


def resolve_utc_date(today: date | datetime | None = None) -> date:
    """Resolve a deterministic UTC calendar date for daily quota tracking."""
    if today is None:
        return datetime.now(UTC).date()
    if isinstance(today, datetime):
        if today.tzinfo is None or today.utcoffset() is None:
            raise ValueError("today datetime must be timezone-aware.")
        return today.astimezone(UTC).date()
    if isinstance(today, date):
        return today
    raise TypeError(
        f"today must be a date, timezone-aware datetime, or None; got {type(today).__name__}.",
    )


def _validate_user_id(user_id: object) -> int:
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise ValueError(f"user_id must be an integer, got {user_id!r}.")
    if not (MIN_TELEGRAM_USER_ID <= user_id <= MAX_TELEGRAM_USER_ID):
        raise ValueError(
            f"user_id must be between {MIN_TELEGRAM_USER_ID} and {MAX_TELEGRAM_USER_ID}, "
            f"got {user_id}.",
        )
    return int(user_id)


def _resolve_daily_limit(
    daily_limit: int | None,
    settings: Settings | None,
) -> int:
    if daily_limit is None:
        resolved_settings = settings if settings is not None else get_settings()
        return resolved_settings.daily_llm_limit
    if isinstance(daily_limit, bool) or not isinstance(daily_limit, int):
        raise ValueError(f"daily_limit must be an integer, got {daily_limit!r}.")
    if daily_limit < 0:
        raise ValueError(f"daily_limit must be non-negative, got {daily_limit}.")
    return int(daily_limit)


async def check_and_increment_llm_quota(
    db: AsyncSession,
    user_id: int,
    *,
    daily_limit: int | None = None,
    settings: Settings | None = None,
    today: date | datetime | None = None,
) -> LLMQuotaStatus:
    """Atomically enforce and increment a user's daily LLM request quota.

    Acquires a row-level ``SELECT ... FOR UPDATE`` lock on ``users`` to prevent race
    conditions across concurrent requests for the same ``user_id``. Automatically resets
    ``daily_llm_calls = 0`` and updates ``llm_calls_reset_date`` whenever
    ``llm_calls_reset_date`` is ``NULL`` or strictly earlier than ``today`` (UTC).

    Flushes changes within the caller's transaction without committing so the caller
    retains transactional control. Raises ``DailyLLMLimitExceededError`` (HTTP 429)
    without exceeding the limit whenever ``daily_llm_calls >= daily_limit``, including
    when ``daily_limit == 0``.
    """
    validated_user_id = _validate_user_id(user_id)
    resolved_today = resolve_utc_date(today)
    resolved_limit = _resolve_daily_limit(daily_limit, settings)

    stmt = (
        select(User)
        .where(User.telegram_id == validated_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        user, _ = await ensure_user_initialized(db, validated_user_id, for_update=True)

    needs_reset = (
        user.llm_calls_reset_date is None
        or user.llm_calls_reset_date < resolved_today
    )
    if needs_reset:
        user.daily_llm_calls = 0
        user.llm_calls_reset_date = resolved_today

    effective_reset_date = (
        user.llm_calls_reset_date if user.llm_calls_reset_date is not None else resolved_today
    )

    if user.daily_llm_calls >= resolved_limit:
        if needs_reset:
            await db.flush()
        raise DailyLLMLimitExceededError(
            user_id=validated_user_id,
            daily_llm_calls=user.daily_llm_calls,
            daily_llm_limit=resolved_limit,
            reset_date=effective_reset_date,
        )

    user.daily_llm_calls += 1
    await db.flush()

    return LLMQuotaStatus(
        user_id=validated_user_id,
        daily_llm_calls=user.daily_llm_calls,
        daily_llm_limit=resolved_limit,
        remaining=resolved_limit - user.daily_llm_calls,
        reset_date=effective_reset_date,
    )


async def enforce_daily_llm_limit(
    request: Request,
    user_id: CurrentUserId,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    now: Annotated[datetime, Depends(get_utc_now)],
) -> LLMQuotaStatus:
    """FastAPI dependency enforcing the authenticated user's daily LLM quota."""
    resolved_settings = settings
    if get_settings not in request.app.dependency_overrides:
        app_settings = getattr(request.app.state, "settings", None)
        if isinstance(app_settings, Settings):
            resolved_settings = app_settings

    return await check_and_increment_llm_quota(
        db,
        user_id,
        settings=resolved_settings,
        today=now,
    )


__all__ = [
    "DEFAULT_RATE_LIMIT_DETAIL_UA",
    "DailyLLMLimitExceededError",
    "LLMQuotaStatus",
    "check_and_increment_llm_quota",
    "enforce_daily_llm_limit",
    "resolve_utc_date",
]
