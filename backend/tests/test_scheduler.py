"""Unit and PostgreSQL integration tests for the standalone scheduler service."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.scoring import build_radar_axes, compute_user_radar
from app.db.models import (
    Attempt,
    Deck,
    TrainingSession,
    User,
    UserWordFSRS,
    WeeklySnapshot,
    Word,
)
from app.scheduler.jobs import (
    format_daily_reminder_message,
    format_weekly_report_message,
    get_week_start_utc,
    resolve_user_timezone,
    run_daily_reminders,
    run_weekly_reports,
)
from app.scheduler.main import (
    DAILY_REMINDERS_JOB_ID,
    WEEKLY_REPORTS_JOB_ID,
    build_scheduler,
    configure_scheduler_logging,
    run_scheduler,
)
from app.scheduler.notifier import (
    DEFAULT_TELEGRAM_API_BASE_URL,
    MAX_TELEGRAM_TIMEOUT_SECONDS,
    TelegramConfigError,
    TelegramDeliveryError,
    TelegramNotifier,
    TokenRedactionFilter,
    escape_telegram_html,
    redact_bot_token,
)
from app.schemas.stats import RadarStatsResponse, WeakTopicItem

FAKE_BOT_TOKEN = "999999999:AAFakeSecretSchedulerTokenForTests"
TELEGRAM_SEND_URL = f"{DEFAULT_TELEGRAM_API_BASE_URL}/bot{FAKE_BOT_TOKEN}/sendMessage"
BACKEND_DIR = Path(__file__).resolve().parent.parent

WINTER_UTC = datetime(2026, 1, 15, 17, 0, 0, tzinfo=UTC)
SUMMER_DST_UTC = datetime(2026, 7, 15, 16, 0, 0, tzinfo=UTC)
SUNDAY_EVENING_UTC = datetime(2026, 5, 10, 18, 0, 0, tzinfo=UTC)
SUNDAY_WEEK_START = date(2026, 5, 4)
PREVIOUS_WEEK_START = date(2026, 4, 27)


async def _seed_user(
    db: AsyncSession,
    *,
    telegram_id: int,
    reminder_hour: int = 19,
    timezone: str = "Europe/Kyiv",
    level: str = "B1",
) -> User:
    user = User(
        telegram_id=telegram_id,
        reminder_hour=reminder_hour,
        timezone=timezone,
        level=level,
    )
    db.add(user)
    await db.flush()
    return user


async def _seed_user_words(
    db: AsyncSession,
    *,
    user_id: int,
    cards: list[tuple[str, datetime, float, int]],
) -> None:
    """Seed a deck + words + FSRS cards (`(word_de, due_at, stability, reps)`)."""
    deck = Deck(owner_id=user_id, title=f"Deck {user_id}", level="mixed")
    db.add(deck)
    await db.flush()

    for idx, (word_de, due_at, stability, reps) in enumerate(cards, start=1):
        word = Word(
            deck_id=deck.id,
            word_de=word_de,
            translation_ua=f"переклад {idx}",
            source="text",
        )
        db.add(word)
        await db.flush()
        db.add(
            UserWordFSRS(
                user_id=user_id,
                word_id=word.id,
                state=2 if reps > 0 else 0,
                stability=stability,
                difficulty=5.0,
                due_at=due_at,
                reps=reps,
                lapses=0,
                last_review=due_at - timedelta(days=1) if reps > 0 else None,
            ),
        )
    await db.flush()


async def _seed_attempts(
    db: AsyncSession,
    *,
    user_id: int,
    attempts_data: list[tuple[str, int, str | None, datetime, str, list[dict[str, int]]]],
) -> None:
    """Seed a completed session and attempts for radar scoring."""
    session = TrainingSession(
        user_id=user_id,
        mode="mix",
        total_exercises=max(1, len(attempts_data)),
        status="completed",
        created_at=attempts_data[0][3] if attempts_data else SUNDAY_EVENING_UTC,
    )
    db.add(session)
    await db.flush()

    for grammar_topic, grammar_score, youtube_topic, created_at, level, ratings in attempts_data:
        db.add(
            Attempt(
                session_id=session.id,
                user_id=user_id,
                word_ids=[1],
                exercise_type="translation",
                level=level,
                grammar_topic=grammar_topic,
                question_payload={"task_text": "Test", "reference_answer": "Test"},
                user_answer="Meine Antwort",
                used_hint=False,
                is_correct=grammar_score >= 70,
                grammar_score=grammar_score,
                fsrs_ratings=ratings,
                youtube_topic=youtube_topic,
                created_at=created_at,
            ),
        )
    await db.flush()


# ---------------------------------------------------------------------------
# Unit tests: TelegramNotifier, escaping, token redaction, time helpers
# ---------------------------------------------------------------------------


def test_notifier_fails_controllably_when_bot_token_missing() -> None:
    with pytest.raises(TelegramConfigError, match="BOT_TOKEN"):
        TelegramNotifier(None)

    with pytest.raises(TelegramConfigError, match="BOT_TOKEN"):
        TelegramNotifier("   ")


def test_notifier_validates_timeout_and_hides_token_in_repr() -> None:
    notifier = TelegramNotifier(FAKE_BOT_TOKEN)
    assert notifier.timeout_seconds <= MAX_TELEGRAM_TIMEOUT_SECONDS
    assert FAKE_BOT_TOKEN not in repr(notifier)

    with pytest.raises(ValueError, match="timeout_seconds"):
        TelegramNotifier(FAKE_BOT_TOKEN, timeout_seconds=0)

    with pytest.raises(ValueError, match="timeout_seconds"):
        TelegramNotifier(FAKE_BOT_TOKEN, timeout_seconds=10.5)


def test_escape_and_redaction_helpers() -> None:
    assert escape_telegram_html("A < B & C > D") == "A &lt; B &amp; C &gt; D"
    raw = f"POST {TELEGRAM_SEND_URL} failed with token {FAKE_BOT_TOKEN}"
    redacted = redact_bot_token(raw, FAKE_BOT_TOKEN)
    assert FAKE_BOT_TOKEN not in redacted
    assert "/bot[REDACTED]/sendMessage" in redacted

    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Request to %s failed",
        args=(TELEGRAM_SEND_URL,),
        exc_info=None,
    )
    token_filter = TokenRedactionFilter(FAKE_BOT_TOKEN)
    assert token_filter.filter(record) is True
    assert FAKE_BOT_TOKEN not in record.getMessage()


@pytest.mark.asyncio
async def test_notifier_escapes_plain_text_and_never_leaks_token_on_errors() -> None:
    with respx.mock(assert_all_called=True) as mock_http:
        send_route = mock_http.post(TELEGRAM_SEND_URL).mock(
            side_effect=[
                httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
                httpx.Response(
                    403,
                    json={
                        "ok": False,
                        "error_code": 403,
                        "description": f"Forbidden: bot {FAKE_BOT_TOKEN} was blocked by the user",
                    },
                ),
                httpx.Response(
                    429,
                    json={
                        "ok": False,
                        "error_code": 429,
                        "description": "Too Many Requests: retry after 12",
                        "parameters": {"retry_after": 12},
                    },
                ),
                httpx.Response(502, text="Bad Gateway"),
                httpx.ReadTimeout("timed out"),
            ],
        )

        async with TelegramNotifier(FAKE_BOT_TOKEN, timeout_seconds=5.0) as notifier:
            # 1. Success with automatic HTML escaping
            resp = await notifier.send_message(chat_id=101, text="Тест <script> & > 0")
            assert resp["ok"] is True
            sent_payload = json.loads(send_route.calls[0].request.content.decode("utf-8"))
            assert sent_payload["chat_id"] == 101
            assert sent_payload["parse_mode"] == "HTML"
            assert sent_payload["text"] == "Тест &lt;script&gt; &amp; &gt; 0"

            # 2. 403 Forbidden
            with pytest.raises(TelegramDeliveryError) as exc_403:
                await notifier.send_message(chat_id=102, text="hi")
            assert exc_403.value.status_code == 403
            assert exc_403.value.reason == "forbidden"
            assert exc_403.value.__cause__ is None
            assert FAKE_BOT_TOKEN not in str(exc_403.value)

            # 3. 429 Rate limited
            with pytest.raises(TelegramDeliveryError) as exc_429:
                await notifier.send_message(chat_id=103, text="hi")
            assert exc_429.value.status_code == 429
            assert exc_429.value.reason == "rate_limited"
            assert exc_429.value.retry_after == 12
            assert FAKE_BOT_TOKEN not in str(exc_429.value)

            # 4. 5xx Server error
            with pytest.raises(TelegramDeliveryError) as exc_502:
                await notifier.send_message(chat_id=104, text="hi")
            assert exc_502.value.status_code == 502
            assert exc_502.value.reason == "server_error"
            assert FAKE_BOT_TOKEN not in str(exc_502.value)

            # 5. Timeout
            with pytest.raises(TelegramDeliveryError) as exc_timeout:
                await notifier.send_message(chat_id=105, text="hi")
            assert exc_timeout.value.status_code is None
            assert exc_timeout.value.reason == "timeout"
            assert exc_timeout.value.__cause__ is None
            assert FAKE_BOT_TOKEN not in str(exc_timeout.value)


def test_timezone_fallback_week_start_and_message_formatting() -> None:
    assert resolve_user_timezone("Europe/Kyiv").key == "Europe/Kyiv"  # type: ignore[attr-defined]
    assert resolve_user_timezone("Invalid/Timezone") == UTC
    assert resolve_user_timezone("") == UTC
    assert resolve_user_timezone(None) == UTC

    # Canonical Monday UTC across different days of the same week
    assert get_week_start_utc(datetime(2026, 5, 4, 0, 5, tzinfo=UTC)) == date(2026, 5, 4)
    assert get_week_start_utc(datetime(2026, 5, 6, 13, 0, tzinfo=UTC)) == date(2026, 5, 4)
    assert get_week_start_utc(datetime(2026, 5, 10, 23, 59, tzinfo=UTC)) == date(2026, 5, 4)

    # Ukrainian pluralization checks
    assert "дозріло <b>1</b> слово." in format_daily_reminder_message(1)
    assert "дозріли <b>2</b> слова." in format_daily_reminder_message(2)
    assert "дозріли <b>4</b> слова." in format_daily_reminder_message(4)
    assert "дозріло <b>5</b> слів." in format_daily_reminder_message(5)
    assert "дозріло <b>11</b> слів." in format_daily_reminder_message(11)
    assert "дозріло <b>21</b> слово." in format_daily_reminder_message(21)

    with pytest.raises(ValueError, match="positive"):
        format_daily_reminder_message(0)

    # Weekly report escaping of special characters in weak topics
    radar = RadarStatsResponse(
        axes=build_radar_axes(vocab_score=40, grammar_score=65, consistency_score=80),
        weak_topics=[
            WeakTopicItem(
                grammar_topic="Kasus: Dativ & Akkusativ <B1>",
                avg_grammar_score=58.5,
                attempts_count=3,
                youtube_topic="Dativ & Akkusativ <приклади>",
            ),
        ],
    )
    msg = format_weekly_report_message(radar, week_start=SUNDAY_WEEK_START, previous_snapshot=None)
    assert "Kasus: Dativ &amp; Akkusativ &lt;B1&gt;" in msg
    assert "Dativ &amp; Akkusativ &lt;приклади&gt;" in msg


# ---------------------------------------------------------------------------
# PostgreSQL integration tests: run_daily_reminders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_reminders_filters_by_timezone_and_dst_and_skips_zero_due(
    db_session: AsyncSession,
) -> None:
    # Winter check: 2026-01-15 17:00 UTC ->
    #   Europe/Kyiv (UTC+2) = 19:00
    #   Europe/Berlin (UTC+1) = 18:00
    #   America/New_York (UTC-5) = 12:00
    #   Corrupted TZ -> UTC = 17:00
    u_kyiv_due = 7_100_000_001
    u_berlin_due = 7_100_000_002
    u_ny_due = 7_100_000_003
    u_kyiv_zero_due = 7_100_000_004
    u_berlin_wrong_hour = 7_100_000_005
    u_bad_tz_utc_match = 7_100_000_006

    await _seed_user(db_session, telegram_id=u_kyiv_due, reminder_hour=19, timezone="Europe/Kyiv")
    await _seed_user(
        db_session,
        telegram_id=u_berlin_due,
        reminder_hour=18,
        timezone="Europe/Berlin",
    )
    await _seed_user(
        db_session,
        telegram_id=u_ny_due,
        reminder_hour=12,
        timezone="America/New_York",
    )
    await _seed_user(
        db_session,
        telegram_id=u_kyiv_zero_due,
        reminder_hour=19,
        timezone="Europe/Kyiv",
    )
    await _seed_user(
        db_session,
        telegram_id=u_berlin_wrong_hour,
        reminder_hour=19,
        timezone="Europe/Berlin",
    )
    await _seed_user(
        db_session,
        telegram_id=u_bad_tz_utc_match,
        reminder_hour=17,
        timezone="Broken/Zone_Name",
    )

    # Seed FSRS cards: strictly isolated by user_id and due_at <= now
    await _seed_user_words(
        db_session,
        user_id=u_kyiv_due,
        cards=[
            ("der Zug", WINTER_UTC - timedelta(hours=2), 5.0, 2),
            ("die Reise", WINTER_UTC, 4.0, 1),  # due_at == now counts as due
            ("das Buch", WINTER_UTC + timedelta(minutes=1), 10.0, 3),  # future -> not due
        ],
    )
    await _seed_user_words(
        db_session,
        user_id=u_berlin_due,
        cards=[("die Arbeit", WINTER_UTC - timedelta(minutes=30), 3.0, 1)],
    )
    await _seed_user_words(
        db_session,
        user_id=u_ny_due,
        cards=[
            ("das Haus", WINTER_UTC - timedelta(days=1), 2.0, 1),
            ("die Zeit", WINTER_UTC - timedelta(hours=3), 2.0, 1),
            ("der Tisch", WINTER_UTC - timedelta(hours=1), 2.0, 1),
        ],
    )
    # Only future card -> 0 due words -> must NOT be notified
    await _seed_user_words(
        db_session,
        user_id=u_kyiv_zero_due,
        cards=[("der Apfel", WINTER_UTC + timedelta(hours=5), 10.0, 2)],
    )
    # Has due card, but wrong local hour (18 != 19 in Berlin winter)
    await _seed_user_words(
        db_session,
        user_id=u_berlin_wrong_hour,
        cards=[("die Schule", WINTER_UTC - timedelta(hours=1), 3.0, 1)],
    )
    await _seed_user_words(
        db_session,
        user_id=u_bad_tz_utc_match,
        cards=[("der Freund", WINTER_UTC - timedelta(minutes=5), 2.0, 1)],
    )

    with respx.mock(assert_all_called=True) as mock_http:
        route = mock_http.post(TELEGRAM_SEND_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 10}}),
        )
        async with TelegramNotifier(FAKE_BOT_TOKEN) as notifier:
            winter_summary = await run_daily_reminders(db_session, notifier, now=WINTER_UTC)

    assert winter_summary.matched_users == 5
    assert winter_summary.sent == 4
    assert winter_summary.skipped_no_due == 1
    assert winter_summary.failed == 0
    assert set(winter_summary.sent_user_ids) == {
        u_kyiv_due,
        u_berlin_due,
        u_ny_due,
        u_bad_tz_utc_match,
    }

    payloads_by_chat = {
        json.loads(call.request.content.decode("utf-8"))["chat_id"]: json.loads(
            call.request.content.decode("utf-8"),
        )["text"]
        for call in route.calls
    }
    assert "<b>2</b> слова" in payloads_by_chat[u_kyiv_due]
    assert "<b>1</b> слово" in payloads_by_chat[u_berlin_due]
    assert "<b>3</b> слова" in payloads_by_chat[u_ny_due]
    assert "<b>1</b> слово" in payloads_by_chat[u_bad_tz_utc_match]
    assert u_kyiv_zero_due not in payloads_by_chat
    assert u_berlin_wrong_hour not in payloads_by_chat

    # Summer DST check: 2026-07-15 16:00 UTC ->
    #   Europe/Kyiv (UTC+3) = 19:00 (matches u_kyiv_due)
    #   Europe/Berlin (UTC+2) = 18:00 (matches u_berlin_due, NOT u_berlin_wrong_hour=19)
    #   America/New_York (UTC-4) = 12:00 (matches u_ny_due)
    #   Broken TZ (UTC) = 16:00 (does NOT match reminder_hour=17)
    with respx.mock(assert_all_called=True) as mock_http:
        dst_route = mock_http.post(TELEGRAM_SEND_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 20}}),
        )
        async with TelegramNotifier(FAKE_BOT_TOKEN) as notifier:
            summer_summary = await run_daily_reminders(db_session, notifier, now=SUMMER_DST_UTC)

    # u_kyiv_zero_due's card from winter is now due in summer too, so 4 matched and all 4 due
    assert summer_summary.matched_users == 4
    assert summer_summary.sent == 4
    assert summer_summary.failed == 0
    assert set(summer_summary.sent_user_ids) == {
        u_kyiv_due,
        u_berlin_due,
        u_ny_due,
        u_kyiv_zero_due,
    }
    assert len(dst_route.calls) == 4


@pytest.mark.asyncio
async def test_daily_reminders_isolates_user_failures_without_leaking_token(
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    u_403 = 7_100_000_101
    u_429 = 7_100_000_102
    u_500 = 7_100_000_103
    u_timeout = 7_100_000_104
    u_ok = 7_100_000_105

    for uid in (u_403, u_429, u_500, u_timeout, u_ok):
        await _seed_user(db_session, telegram_id=uid, reminder_hour=19, timezone="Europe/Kyiv")
        await _seed_user_words(
            db_session,
            user_id=uid,
            cards=[(f"das Wort {uid}", WINTER_UTC - timedelta(hours=1), 3.0, 1)],
        )

    def _dispatch(request: httpx.Request) -> httpx.Response:
        chat_id = json.loads(request.content.decode("utf-8"))["chat_id"]
        if chat_id == u_403:
            return httpx.Response(
                403,
                json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"},
            )
        if chat_id == u_429:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 30},
                },
            )
        if chat_id == u_500:
            return httpx.Response(500, json={"ok": False, "description": "Internal Server Error"})
        if chat_id == u_timeout:
            raise httpx.ReadTimeout("simulated timeout", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    caplog.set_level(logging.WARNING)
    with respx.mock(assert_all_called=True) as mock_http:
        mock_http.post(TELEGRAM_SEND_URL).mock(side_effect=_dispatch)
        async with TelegramNotifier(FAKE_BOT_TOKEN) as notifier:
            summary = await run_daily_reminders(db_session, notifier, now=WINTER_UTC)

    assert summary.matched_users == 5
    assert summary.sent == 1
    assert summary.failed == 4
    assert summary.sent_user_ids == (u_ok,)
    assert set(summary.failed_user_ids) == {u_403, u_429, u_500, u_timeout}
    assert FAKE_BOT_TOKEN not in caplog.text


# ---------------------------------------------------------------------------
# PostgreSQL integration tests: run_weekly_reports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weekly_reports_before_after_first_week_empty_weak_topics_and_idempotency(
    db_session: AsyncSession,
) -> None:
    u_with_prev = 7_200_000_001
    u_first_week = 7_200_000_002
    u_no_weak = 7_200_000_003

    await _seed_user(db_session, telegram_id=u_with_prev, level="B1")
    await _seed_user(db_session, telegram_id=u_first_week, level="A2")
    await _seed_user(db_session, telegram_id=u_no_weak, level="B1")

    # User 1: has previous week snapshot + weak topics + reviewed words
    db_session.add(
        WeeklySnapshot(
            user_id=u_with_prev,
            week_start=PREVIOUS_WEEK_START,
            vocab_score=10,
            grammar_score=40,
            consistency_score=90,
            writing_score=0,
            listening_score=0,
        ),
    )
    await _seed_user_words(
        db_session,
        user_id=u_with_prev,
        cards=[
            ("die Verspätung", SUNDAY_EVENING_UTC + timedelta(days=3), 21.0, 4),
            ("das Treffen", SUNDAY_EVENING_UTC + timedelta(days=2), 14.0, 3),
        ],
    )
    await _seed_attempts(
        db_session,
        user_id=u_with_prev,
        attempts_data=[
            (
                "Nebensätze mit weil",
                55,
                "Порядок слів у реченні з weil німецька B1",
                SUNDAY_EVENING_UTC - timedelta(days=2),
                "B1",
                [{"word_id": 1, "fsrs_rating": 3}],
            ),
            (
                "Perfekt mit sein",
                70,
                "Perfekt mit sein та haben німецька",
                SUNDAY_EVENING_UTC - timedelta(days=3),
                "B1",
                [{"word_id": 2, "fsrs_rating": 4}],
            ),
            (
                "Wechselpräpositionen",
                80,
                "Прийменники подвійного керування німецька",
                SUNDAY_EVENING_UTC - timedelta(days=4),
                "B1",
                [{"word_id": 1, "fsrs_rating": 3}],
            ),
            (
                "Konjunktiv II",
                90,
                "Konjunktiv II ввічливі прохання",
                SUNDAY_EVENING_UTC - timedelta(days=5),
                "B1",
                [{"word_id": 2, "fsrs_rating": 4}],
            ),
        ],
    )

    # User 2: first week (no prior snapshot), 1 weak topic
    await _seed_attempts(
        db_session,
        user_id=u_first_week,
        attempts_data=[
            (
                "Modalverben",
                60,
                "Модальні дієслова німецька A2",
                SUNDAY_EVENING_UTC - timedelta(days=1),
                "A2",
                [{"word_id": 1, "fsrs_rating": 3}],
            ),
        ],
    )

    # User 3: first week with perfect 100 grammar score -> empty weak_topics
    await _seed_attempts(
        db_session,
        user_id=u_no_weak,
        attempts_data=[
            (
                "Präsens",
                100,
                "Präsens німецька",
                SUNDAY_EVENING_UTC - timedelta(days=1),
                "B1",
                [{"word_id": 1, "fsrs_rating": 4}],
            ),
        ],
    )
    await db_session.commit()

    expected_radar_1 = await compute_user_radar(db_session, u_with_prev, now=SUNDAY_EVENING_UTC)
    expected_scores_1 = expected_radar_1.scores_by_axis()

    with respx.mock(assert_all_called=True) as mock_http:
        route = mock_http.post(TELEGRAM_SEND_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 100}}),
        )
        async with TelegramNotifier(FAKE_BOT_TOKEN) as notifier:
            first_run = await run_weekly_reports(db_session, notifier, now=SUNDAY_EVENING_UTC)

    assert first_run.week_start == SUNDAY_WEEK_START
    assert first_run.total_users == 3
    assert first_run.snapshots_created == 3
    assert first_run.sent == 3
    assert first_run.skipped_existing == 0
    assert first_run.failed == 0

    messages = {
        json.loads(call.request.content.decode("utf-8"))["chat_id"]: json.loads(
            call.request.content.decode("utf-8"),
        )["text"]
        for call in route.calls
    }

    # Verify User 1: before / after comparison + top-3 weak topics (4th excluded)
    msg_1 = messages[u_with_prev]
    assert "було 10 → стало" in msg_1
    assert f"було 10 → стало {expected_scores_1['vocab']}" in msg_1
    assert f"було 40 → стало {expected_scores_1['grammar']}" in msg_1
    assert f"було 90 → стало {expected_scores_1['consistency']}" in msg_1
    assert "Словник" in msg_1
    assert "Граматика" in msg_1
    assert "Стабільність" in msg_1
    assert "Порядок слів у реченні з weil німецька B1" in msg_1
    assert "Perfekt mit sein та haben німецька" in msg_1
    assert "Прийменники подвійного керування німецька" in msg_1
    assert "Konjunktiv II" not in msg_1  # Only top-3 included

    # Verify User 2: first week without previous snapshot
    msg_2 = messages[u_first_week]
    assert "перший зафіксований знімок" in msg_2
    assert "було " not in msg_2
    assert "Словник" in msg_2
    assert "Граматика" in msg_2
    assert "Стабільність" in msg_2
    assert "Модальні дієслова німецька A2" in msg_2

    # Verify User 3: empty weak_topics positive summary
    msg_3 = messages[u_no_weak]
    assert "Слабких тем за останні 14 днів не виявлено" in msg_3

    # Verify persisted snapshot values match compute_user_radar
    snapshots_u1 = (
        await db_session.execute(
            select(WeeklySnapshot).where(
                WeeklySnapshot.user_id == u_with_prev,
                WeeklySnapshot.week_start == SUNDAY_WEEK_START,
            ),
        )
    ).scalars().all()
    assert len(snapshots_u1) == 1
    assert snapshots_u1[0].vocab_score == expected_scores_1["vocab"]
    assert snapshots_u1[0].grammar_score == expected_scores_1["grammar"]
    assert snapshots_u1[0].consistency_score == expected_scores_1["consistency"]

    # Idempotency check: re-run within the SAME week_start (2 hours later on same Sunday)
    with respx.mock(assert_all_called=False) as mock_http:
        second_route = mock_http.post(TELEGRAM_SEND_URL).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 200}}),
        )
        async with TelegramNotifier(FAKE_BOT_TOKEN) as notifier:
            second_run = await run_weekly_reports(
                db_session,
                notifier,
                now=SUNDAY_EVENING_UTC + timedelta(hours=2),
            )

    assert second_run.week_start == SUNDAY_WEEK_START
    assert second_run.snapshots_created == 0
    assert second_run.sent == 0
    assert second_run.skipped_existing == 3
    assert second_run.failed == 0
    assert second_route.call_count == 0

    all_week_snapshots = (
        await db_session.execute(
            select(WeeklySnapshot).where(WeeklySnapshot.week_start == SUNDAY_WEEK_START),
        )
    ).scalars().all()
    assert len(all_week_snapshots) == 3


@pytest.mark.asyncio
async def test_weekly_reports_isolates_telegram_errors_and_continues(
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    u_blocked = 7_200_000_201
    u_server_err = 7_200_000_202
    u_timeout = 7_200_000_203
    u_healthy = 7_200_000_204

    for uid in (u_blocked, u_server_err, u_timeout, u_healthy):
        await _seed_user(db_session, telegram_id=uid)
    await db_session.commit()

    def _dispatch(request: httpx.Request) -> httpx.Response:
        chat_id = json.loads(request.content.decode("utf-8"))["chat_id"]
        if chat_id == u_blocked:
            return httpx.Response(403, json={"ok": False, "description": "Forbidden"})
        if chat_id == u_server_err:
            return httpx.Response(503, json={"ok": False, "description": "Service Unavailable"})
        if chat_id == u_timeout:
            raise httpx.ConnectTimeout("timeout", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 300}})

    caplog.set_level(logging.WARNING)
    with respx.mock(assert_all_called=True) as mock_http:
        mock_http.post(TELEGRAM_SEND_URL).mock(side_effect=_dispatch)
        async with TelegramNotifier(FAKE_BOT_TOKEN) as notifier:
            summary = await run_weekly_reports(db_session, notifier, now=SUNDAY_EVENING_UTC)

    assert summary.total_users == 4
    assert summary.snapshots_created == 4
    assert summary.sent == 1
    assert summary.failed == 3
    assert summary.sent_user_ids == (u_healthy,)
    assert set(summary.failed_user_ids) == {u_blocked, u_server_err, u_timeout}
    assert FAKE_BOT_TOKEN not in caplog.text


# ---------------------------------------------------------------------------
# Scheduler wiring & FastAPI isolation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_scheduler_configures_utc_cron_jobs_and_clean_shutdown(
    migrated_test_db_url: str,
) -> None:
    configure_scheduler_logging("INFO", FAKE_BOT_TOKEN)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING

    async with TelegramNotifier(FAKE_BOT_TOKEN) as notifier:
        scheduler = build_scheduler(session_factory=None, notifier=notifier)  # type: ignore[arg-type]
        assert scheduler.timezone == UTC

        jobs = {job.id: job for job in scheduler.get_jobs()}
        assert set(jobs) == {DAILY_REMINDERS_JOB_ID, WEEKLY_REPORTS_JOB_ID}

        daily_job = jobs[DAILY_REMINDERS_JOB_ID]
        assert isinstance(daily_job.trigger, CronTrigger)
        assert daily_job.trigger.timezone == UTC
        assert daily_job.max_instances == 1
        assert daily_job.coalesce is True

        weekly_job = jobs[WEEKLY_REPORTS_JOB_ID]
        assert isinstance(weekly_job.trigger, CronTrigger)
        assert weekly_job.trigger.timezone == UTC
        assert weekly_job.max_instances == 1
        assert weekly_job.coalesce is True

    # Verify run_scheduler starts & shuts down cleanly when stop_event triggers
    stop_event = asyncio.Event()
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=migrated_test_db_url,
        bot_token=FAKE_BOT_TOKEN,
    )

    async def _signal_stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    stopper = asyncio.create_task(_signal_stop_soon())
    await run_scheduler(settings=settings, stop_event=stop_event)
    await stopper


@pytest.mark.asyncio
async def test_run_scheduler_fails_controllably_without_bot_token() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/deutsch_learning_test",
        bot_token=None,
    )
    with pytest.raises(TelegramConfigError, match="BOT_TOKEN"):
        await run_scheduler(settings=settings)


def test_fastapi_app_never_imports_or_starts_apscheduler() -> None:
    # Static check: backend/app/main.py has zero imports of apscheduler or app.scheduler
    main_source = (BACKEND_DIR / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any(
        mod == "apscheduler"
        or mod.startswith("apscheduler.")
        or mod == "app.scheduler"
        or mod.startswith("app.scheduler.")
        for mod in imported_modules
    )

    # Runtime check in a fresh Python interpreter: importing & creating FastAPI app
    # must NOT load apscheduler or app.scheduler into sys.modules.
    check_code = (
        "import sys; "
        "from app.config import Settings; "
        "from app.main import create_app; "
        "create_app(Settings(_env_file=None, app_env='test')); "
        "bad = [m for m in sys.modules if m == 'apscheduler' or m.startswith('apscheduler.') "
        "or m == 'app.scheduler' or m.startswith('app.scheduler.')]; "
        "assert not bad, f'Unexpected scheduler imports in FastAPI app: {bad}'"
    )
    result = subprocess.run(
        [sys.executable, "-c", check_code],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"STDERR:\n{result.stderr}\nSTDOUT:\n{result.stdout}"
