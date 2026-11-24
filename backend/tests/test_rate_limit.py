"""Unit and PostgreSQL integration tests for daily per-user LLM quota enforcement."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Annotated, Any

import pytest
from fastapi import Depends, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from app.api.deps import get_utc_now
from app.config import Settings, get_settings
from app.core.rate_limit import (
    DEFAULT_RATE_LIMIT_DETAIL_UA,
    DailyLLMLimitExceededError,
    LLMQuotaStatus,
    check_and_increment_llm_quota,
    enforce_daily_llm_limit,
    resolve_utc_date,
)
from app.db.base import create_async_db_engine, create_session_factory, get_db
from app.db.models import User
from app.main import create_app

DAY_1 = date(2026, 5, 1)
DAY_2 = date(2026, 5, 2)
DAY_3 = date(2026, 5, 3)

USER_A_ID = 6_100_000_001  # > 2^31 - 1 (64-bit BIGINT)
USER_B_ID = 6_100_000_002
USER_CONCURRENT_LIMIT_ID = 6_900_000_201
USER_CONCURRENT_RESET_ID = 6_900_000_202

TEST_INTERNAL_API_KEY = "rate-limit-test-internal-api-key-123"


def _bot_headers(telegram_id: int) -> dict[str, str]:
    return {
        "X-Internal-API-Key": TEST_INTERNAL_API_KEY,
        "X-Telegram-User-Id": str(telegram_id),
    }


# ---------------------------------------------------------------------------
# Offline unit tests: UTC date resolution, error shape, input validation
# ---------------------------------------------------------------------------


def test_resolve_utc_date_handles_date_aware_datetime_and_default() -> None:
    assert resolve_utc_date(DAY_1) == DAY_1
    assert resolve_utc_date(datetime(2026, 5, 1, 23, 59, 59, tzinfo=UTC)) == DAY_1

    # 2026-05-01 23:30 UTC-02:00 is 2026-05-02 01:30 UTC -> next UTC calendar date
    offset_minus_2 = timezone(timedelta(hours=-2))
    assert resolve_utc_date(datetime(2026, 5, 1, 23, 30, tzinfo=offset_minus_2)) == DAY_2

    # 2026-05-02 01:30 UTC+03:00 is 2026-05-01 22:30 UTC -> previous UTC calendar date
    offset_plus_3 = timezone(timedelta(hours=3))
    assert resolve_utc_date(datetime(2026, 5, 2, 1, 30, tzinfo=offset_plus_3)) == DAY_1

    default_utc = resolve_utc_date(None)
    assert isinstance(default_utc, date)


def test_resolve_utc_date_rejects_naive_datetime_and_invalid_types() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_utc_date(datetime(2026, 5, 1, 12, 0))

    with pytest.raises(TypeError):
        resolve_utc_date("2026-05-01")  # type: ignore[arg-type]


def test_daily_llm_limit_exceeded_error_is_http_429_with_ukrainian_message() -> None:
    err = DailyLLMLimitExceededError(
        user_id=USER_A_ID,
        daily_llm_calls=50,
        daily_llm_limit=50,
        reset_date=DAY_1,
    )

    assert isinstance(err, HTTPException)
    assert err.status_code == 429
    assert err.detail == DEFAULT_RATE_LIMIT_DETAIL_UA
    assert err.user_id == USER_A_ID
    assert err.daily_llm_calls == 50
    assert err.daily_llm_limit == 50
    assert err.reset_date == DAY_1


@pytest.mark.parametrize(
    ("bad_user_id", "bad_limit"),
    [
        (0, 10),
        (-1, 10),
        (True, 10),
        (1 << 63, 10),
        (USER_A_ID, -1),
        (USER_A_ID, False),
        (USER_A_ID, "10"),
    ],
)
async def test_check_and_increment_rejects_invalid_arguments(
    db_session: AsyncSession,
    bad_user_id: Any,
    bad_limit: Any,
) -> None:
    with pytest.raises(ValueError):
        await check_and_increment_llm_quota(
            db_session,
            bad_user_id,
            daily_limit=bad_limit,
            today=DAY_1,
        )


# ---------------------------------------------------------------------------
# PostgreSQL integration tests (isolated TEST_DATABASE_URL)
# ---------------------------------------------------------------------------


async def test_quota_initializes_null_reset_date_and_enforces_limit_with_http_429(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        User(
            telegram_id=USER_A_ID,
            daily_llm_calls=99,  # Stale count with NULL reset date must reset to 0 first
            llm_calls_reset_date=None,
        ),
    )
    await db_session.flush()

    first = await check_and_increment_llm_quota(
        db_session,
        USER_A_ID,
        daily_limit=3,
        today=DAY_1,
    )
    assert first == LLMQuotaStatus(
        user_id=USER_A_ID,
        daily_llm_calls=1,
        daily_llm_limit=3,
        remaining=2,
        reset_date=DAY_1,
    )

    second = await check_and_increment_llm_quota(
        db_session,
        USER_A_ID,
        daily_limit=3,
        today=DAY_1,
    )
    assert second.daily_llm_calls == 2
    assert second.remaining == 1

    third = await check_and_increment_llm_quota(
        db_session,
        USER_A_ID,
        daily_limit=3,
        today=DAY_1,
    )
    assert third.daily_llm_calls == 3
    assert third.remaining == 0

    # 4th and 5th calls on the same UTC date must fail with HTTP 429 without exceeding 3
    for _ in range(2):
        with pytest.raises(DailyLLMLimitExceededError) as exc_info:
            await check_and_increment_llm_quota(
                db_session,
                USER_A_ID,
                daily_limit=3,
                today=DAY_1,
            )
        assert exc_info.value.status_code == 429
        assert exc_info.value.detail == DEFAULT_RATE_LIMIT_DETAIL_UA
        assert exc_info.value.daily_llm_calls == 3
        assert exc_info.value.daily_llm_limit == 3

    persisted = await db_session.get(User, USER_A_ID)
    assert persisted is not None
    assert persisted.daily_llm_calls == 3
    assert persisted.llm_calls_reset_date == DAY_1


async def test_quota_auto_resets_when_reset_date_is_earlier_than_fixed_utc_today(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        User(
            telegram_id=USER_A_ID,
            daily_llm_calls=2,
            llm_calls_reset_date=DAY_1,
        ),
    )
    await db_session.flush()

    # Limit already exhausted on DAY_1
    with pytest.raises(DailyLLMLimitExceededError) as exc_info:
        await check_and_increment_llm_quota(
            db_session,
            USER_A_ID,
            daily_limit=2,
            today=DAY_1,
        )
    assert exc_info.value.status_code == 429

    # Advancing to DAY_2 resets daily_llm_calls to 0 and increments to 1
    next_day_status = await check_and_increment_llm_quota(
        db_session,
        USER_A_ID,
        daily_limit=2,
        today=DAY_2,
    )
    assert next_day_status == LLMQuotaStatus(
        user_id=USER_A_ID,
        daily_llm_calls=1,
        daily_llm_limit=2,
        remaining=1,
        reset_date=DAY_2,
    )

    persisted = await db_session.get(User, USER_A_ID)
    assert persisted is not None
    assert persisted.daily_llm_calls == 1
    assert persisted.llm_calls_reset_date == DAY_2

    # Advancing via timezone-aware datetime crossing UTC midnight resets again to DAY_3
    cross_midnight_dt = datetime(2026, 5, 2, 23, 30, tzinfo=timezone(timedelta(hours=-2)))
    day3_status = await check_and_increment_llm_quota(
        db_session,
        USER_A_ID,
        daily_limit=2,
        today=cross_midnight_dt,
    )
    assert day3_status.daily_llm_calls == 1
    assert day3_status.reset_date == DAY_3


async def test_quota_zero_limit_blocks_immediately_without_incrementing(
    db_session: AsyncSession,
) -> None:
    # Case 1: Fresh user with NULL reset date and 0 calls
    db_session.add(
        User(
            telegram_id=USER_A_ID,
            daily_llm_calls=0,
            llm_calls_reset_date=None,
        ),
    )
    # Case 2: User with previous day's calls > 0
    db_session.add(
        User(
            telegram_id=USER_B_ID,
            daily_llm_calls=15,
            llm_calls_reset_date=DAY_1,
        ),
    )
    await db_session.flush()

    with pytest.raises(DailyLLMLimitExceededError) as exc_a:
        await check_and_increment_llm_quota(
            db_session,
            USER_A_ID,
            daily_limit=0,
            today=DAY_2,
        )
    assert exc_a.value.status_code == 429
    assert exc_a.value.daily_llm_calls == 0
    assert exc_a.value.daily_llm_limit == 0

    with pytest.raises(DailyLLMLimitExceededError) as exc_b:
        await check_and_increment_llm_quota(
            db_session,
            USER_B_ID,
            daily_limit=0,
            today=DAY_2,
        )
    assert exc_b.value.status_code == 429
    assert exc_b.value.daily_llm_calls == 0
    assert exc_b.value.daily_llm_limit == 0

    user_a = await db_session.get(User, USER_A_ID)
    user_b = await db_session.get(User, USER_B_ID)
    assert user_a is not None and user_a.daily_llm_calls == 0
    assert user_a.llm_calls_reset_date == DAY_2
    assert user_b is not None and user_b.daily_llm_calls == 0
    assert user_b.llm_calls_reset_date == DAY_2


async def test_quota_does_not_reset_when_reset_date_is_in_future_or_over_lowered_limit(
    db_session: AsyncSession,
) -> None:
    # Future reset date must not reset counter
    db_session.add(
        User(
            telegram_id=USER_A_ID,
            daily_llm_calls=4,
            llm_calls_reset_date=DAY_3,
        ),
    )
    # Already above a newly lowered limit on the same day must reject without incrementing
    db_session.add(
        User(
            telegram_id=USER_B_ID,
            daily_llm_calls=10,
            llm_calls_reset_date=DAY_2,
        ),
    )
    await db_session.flush()

    status_a = await check_and_increment_llm_quota(
        db_session,
        USER_A_ID,
        daily_limit=5,
        today=DAY_2,
    )
    assert status_a.daily_llm_calls == 5
    assert status_a.reset_date == DAY_3

    with pytest.raises(DailyLLMLimitExceededError) as exc_b:
        await check_and_increment_llm_quota(
            db_session,
            USER_B_ID,
            daily_limit=5,
            today=DAY_2,
        )
    assert exc_b.value.status_code == 429
    assert exc_b.value.daily_llm_calls == 10

    persisted_b = await db_session.get(User, USER_B_ID)
    assert persisted_b is not None
    assert persisted_b.daily_llm_calls == 10


async def test_quota_strict_user_id_isolation_between_user_a_and_user_b(
    db_session: AsyncSession,
) -> None:
    db_session.add_all(
        [
            User(telegram_id=USER_A_ID, daily_llm_calls=0, llm_calls_reset_date=DAY_1),
            User(telegram_id=USER_B_ID, daily_llm_calls=0, llm_calls_reset_date=DAY_1),
        ],
    )
    await db_session.flush()

    # User A exhausts quota (limit=2)
    await check_and_increment_llm_quota(db_session, USER_A_ID, daily_limit=2, today=DAY_1)
    await check_and_increment_llm_quota(db_session, USER_A_ID, daily_limit=2, today=DAY_1)
    with pytest.raises(DailyLLMLimitExceededError) as exc_a:
        await check_and_increment_llm_quota(db_session, USER_A_ID, daily_limit=2, today=DAY_1)
    assert exc_a.value.status_code == 429

    # User B must remain completely unaffected and able to consume quota
    status_b1 = await check_and_increment_llm_quota(
        db_session,
        USER_B_ID,
        daily_limit=2,
        today=DAY_1,
    )
    assert status_b1.user_id == USER_B_ID
    assert status_b1.daily_llm_calls == 1
    assert status_b1.remaining == 1

    # Advancing User B to DAY_2 must not reset User A's counter or date
    status_b2 = await check_and_increment_llm_quota(
        db_session,
        USER_B_ID,
        daily_limit=2,
        today=DAY_2,
    )
    assert status_b2.daily_llm_calls == 1
    assert status_b2.reset_date == DAY_2

    user_a = await db_session.get(User, USER_A_ID)
    user_b = await db_session.get(User, USER_B_ID)
    assert user_a is not None
    assert user_a.daily_llm_calls == 2
    assert user_a.llm_calls_reset_date == DAY_1
    assert user_b is not None
    assert user_b.daily_llm_calls == 1
    assert user_b.llm_calls_reset_date == DAY_2


async def test_quota_uses_settings_daily_llm_limit_and_initializes_missing_user(
    db_session: AsyncSession,
) -> None:
    custom_settings = Settings(_env_file=None, daily_llm_limit=2)

    first = await check_and_increment_llm_quota(
        db_session,
        USER_A_ID,
        settings=custom_settings,
        today=DAY_1,
    )
    assert first.daily_llm_calls == 1
    assert first.daily_llm_limit == 2
    assert first.remaining == 1

    second = await check_and_increment_llm_quota(
        db_session,
        USER_A_ID,
        settings=custom_settings,
        today=DAY_1,
    )
    assert second.daily_llm_calls == 2
    assert second.remaining == 0

    with pytest.raises(DailyLLMLimitExceededError) as exc_info:
        await check_and_increment_llm_quota(
            db_session,
            USER_A_ID,
            settings=custom_settings,
            today=DAY_1,
        )
    assert exc_info.value.status_code == 429


async def test_fastapi_dependency_returns_http_429_ukrainian_and_isolates_users(
    db_session: AsyncSession,
) -> None:
    test_app = create_app(
        settings=Settings(
            _env_file=None,
            app_env="test",
            internal_api_key=TEST_INTERNAL_API_KEY,
            daily_llm_limit=2,
        ),
    )

    current_now = {"value": datetime(2026, 5, 1, 10, 0, tzinfo=UTC)}

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_utc_now] = lambda: current_now["value"]

    @test_app.post("/api/_test/llm-quota")
    async def _probe_quota_route(
        quota: Annotated[LLMQuotaStatus, Depends(enforce_daily_llm_limit)],
    ) -> dict[str, Any]:
        return {
            "user_id": quota.user_id,
            "daily_llm_calls": quota.daily_llm_calls,
            "daily_llm_limit": quota.daily_llm_limit,
            "remaining": quota.remaining,
            "reset_date": quota.reset_date.isoformat(),
        }

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        r1 = await async_client.post("/api/_test/llm-quota", headers=_bot_headers(USER_A_ID))
        assert r1.status_code == 200
        assert r1.json()["daily_llm_calls"] == 1

        r2 = await async_client.post("/api/_test/llm-quota", headers=_bot_headers(USER_A_ID))
        assert r2.status_code == 200
        assert r2.json()["daily_llm_calls"] == 2

        # 3rd call for User A -> HTTP 429 with Ukrainian message
        r3 = await async_client.post("/api/_test/llm-quota", headers=_bot_headers(USER_A_ID))
        assert r3.status_code == 429
        assert r3.json() == {"detail": DEFAULT_RATE_LIMIT_DETAIL_UA}

        # User B is completely unaffected
        rb = await async_client.post("/api/_test/llm-quota", headers=_bot_headers(USER_B_ID))
        assert rb.status_code == 200
        assert rb.json()["user_id"] == USER_B_ID
        assert rb.json()["daily_llm_calls"] == 1

        # Next UTC day resets User A's quota
        current_now["value"] = datetime(2026, 5, 2, 0, 0, 1, tzinfo=UTC)
        r4 = await async_client.post("/api/_test/llm-quota", headers=_bot_headers(USER_A_ID))
        assert r4.status_code == 200
        assert r4.json()["daily_llm_calls"] == 1
        assert r4.json()["reset_date"] == "2026-05-02"


async def test_quota_uses_select_for_update_row_lock(
    db_session: AsyncSession,
) -> None:
    db_session.add(User(telegram_id=USER_A_ID, daily_llm_calls=0, llm_calls_reset_date=DAY_1))
    await db_session.flush()

    executed_sql: list[str] = []
    sync_engine = db_session.get_bind().engine

    def _capture_sql(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        executed_sql.append(statement)

    event.listen(sync_engine, "before_cursor_execute", _capture_sql)
    try:
        await check_and_increment_llm_quota(
            db_session,
            USER_A_ID,
            daily_limit=5,
            today=DAY_1,
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture_sql)

    assert any("FROM users" in sql and "FOR UPDATE" in sql for sql in executed_sql)


async def test_quota_zero_limit_on_brand_new_user_rejects_without_incrementing(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(DailyLLMLimitExceededError) as exc_info:
        await check_and_increment_llm_quota(
            db_session,
            USER_A_ID,
            daily_limit=0,
            today=DAY_1,
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.daily_llm_calls == 0

    persisted = await db_session.get(User, USER_A_ID)
    assert persisted is not None
    assert persisted.daily_llm_calls == 0
    assert persisted.llm_calls_reset_date == DAY_1


async def test_fastapi_dependency_honors_get_settings_override(
    db_session: AsyncSession,
) -> None:
    test_app = create_app(
        settings=Settings(
            _env_file=None,
            app_env="test",
            internal_api_key=TEST_INTERNAL_API_KEY,
            daily_llm_limit=50,
        ),
    )

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_utc_now] = lambda: datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    test_app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        app_env="test",
        internal_api_key=TEST_INTERNAL_API_KEY,
        daily_llm_limit=1,
    )

    @test_app.post("/api/_test/llm-quota-override")
    async def _probe_override_route(
        quota: Annotated[LLMQuotaStatus, Depends(enforce_daily_llm_limit)],
    ) -> dict[str, int]:
        return {"daily_llm_calls": quota.daily_llm_calls, "limit": quota.daily_llm_limit}

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        first = await async_client.post(
            "/api/_test/llm-quota-override",
            headers=_bot_headers(USER_A_ID),
        )
        assert first.status_code == 200
        assert first.json() == {"daily_llm_calls": 1, "limit": 1}

        second = await async_client.post(
            "/api/_test/llm-quota-override",
            headers=_bot_headers(USER_A_ID),
        )
        assert second.status_code == 429
        assert second.json() == {"detail": DEFAULT_RATE_LIMIT_DETAIL_UA}


async def test_concurrent_requests_enforce_exact_quota_with_select_for_update(
    migrated_test_db_url: str,
) -> None:
    """Verify multi-connection PostgreSQL concurrency prevents race-condition overruns."""
    engine = create_async_db_engine(migrated_test_db_url, poolclass=NullPool)
    session_factory = create_session_factory(engine)
    test_ids = (USER_CONCURRENT_LIMIT_ID, USER_CONCURRENT_RESET_ID)

    try:
        async with session_factory() as setup_session:
            await setup_session.execute(delete(User).where(User.telegram_id.in_(test_ids)))
            setup_session.add_all(
                [
                    User(
                        telegram_id=USER_CONCURRENT_LIMIT_ID,
                        daily_llm_calls=0,
                        llm_calls_reset_date=DAY_1,
                    ),
                    User(
                        telegram_id=USER_CONCURRENT_RESET_ID,
                        daily_llm_calls=50,
                        llm_calls_reset_date=DAY_1,
                    ),
                ],
            )
            await setup_session.commit()

        # Scenario 1: 8 concurrent requests on limit=3 on the same UTC day
        async def attempt_same_day() -> tuple[str, int]:
            async with session_factory() as session:
                try:
                    quota = await check_and_increment_llm_quota(
                        session,
                        USER_CONCURRENT_LIMIT_ID,
                        daily_limit=3,
                        today=DAY_1,
                    )
                    await session.commit()
                    return ("ok", quota.daily_llm_calls)
                except DailyLLMLimitExceededError as exc:
                    await session.rollback()
                    return ("429", exc.status_code)

        same_day_results = await asyncio.gather(*[attempt_same_day() for _ in range(8)])
        ok_counts = sorted(val for kind, val in same_day_results if kind == "ok")
        rejected = [val for kind, val in same_day_results if kind == "429"]
        assert ok_counts == [1, 2, 3]
        assert rejected == [429] * 5

        # Scenario 2: 6 concurrent requests crossing midnight UTC rollover on limit=2
        async def attempt_rollover() -> tuple[str, int]:
            async with session_factory() as session:
                try:
                    quota = await check_and_increment_llm_quota(
                        session,
                        USER_CONCURRENT_RESET_ID,
                        daily_limit=2,
                        today=DAY_2,
                    )
                    await session.commit()
                    return ("ok", quota.daily_llm_calls)
                except DailyLLMLimitExceededError as exc:
                    await session.rollback()
                    return ("429", exc.status_code)

        rollover_results = await asyncio.gather(*[attempt_rollover() for _ in range(6)])
        rollover_ok = sorted(val for kind, val in rollover_results if kind == "ok")
        rollover_rejected = [val for kind, val in rollover_results if kind == "429"]
        assert rollover_ok == [1, 2]
        assert rollover_rejected == [429] * 4

        # Verify persisted DB rows directly
        async with session_factory() as verify_session:
            rows = {
                user.telegram_id: user
                for user in (
                    await verify_session.execute(
                        select(User).where(User.telegram_id.in_(test_ids)),
                    )
                )
                .scalars()
                .all()
            }
            assert rows[USER_CONCURRENT_LIMIT_ID].daily_llm_calls == 3
            assert rows[USER_CONCURRENT_LIMIT_ID].llm_calls_reset_date == DAY_1
            assert rows[USER_CONCURRENT_RESET_ID].daily_llm_calls == 2
            assert rows[USER_CONCURRENT_RESET_ID].llm_calls_reset_date == DAY_2
    finally:
        async with session_factory() as cleanup_session:
            await cleanup_session.execute(delete(User).where(User.telegram_id.in_(test_ids)))
            await cleanup_session.commit()
        await engine.dispose()
