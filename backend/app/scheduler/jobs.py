"""Scheduled jobs for hourly FSRS reminders and weekly progress reports."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scoring import compute_user_radar
from app.db.models import User, UserWordFSRS, WeeklySnapshot
from app.scheduler.notifier import (
    TelegramDeliveryError,
    TelegramNotifier,
    escape_telegram_html,
)
from app.schemas.stats import RadarStatsResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DailyRemindersSummary:
    """Execution summary for a single hourly FSRS reminder run."""

    matched_users: int
    sent: int
    skipped_no_due: int
    failed: int
    sent_user_ids: tuple[int, ...] = ()
    failed_user_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class WeeklyReportsSummary:
    """Execution summary for a single weekly progress report run."""

    week_start: date
    total_users: int
    snapshots_created: int
    sent: int
    skipped_existing: int
    failed: int
    sent_user_ids: tuple[int, ...] = ()
    failed_user_ids: tuple[int, ...] = ()


def _ensure_utc(value: datetime, *, field_name: str = "now") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def resolve_user_timezone(timezone_name: str | None) -> tzinfo:
    """Resolve an IANA timezone string with a safe fallback to UTC."""
    candidate = (timezone_name or "").strip()
    if not candidate:
        return UTC
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        logger.warning("Invalid user timezone %r; falling back to UTC", candidate)
        return UTC


def get_week_start_utc(now: datetime) -> date:
    """Return the canonical Monday date of the UTC week containing ``now``."""
    utc_date = _ensure_utc(now, field_name="now").date()
    return utc_date - timedelta(days=utc_date.weekday())


def _pluralize_ukrainian_words(count: int) -> tuple[str, str]:
    """Return grammatically matched ``(verb, noun)`` for ``count`` words in Ukrainian."""
    last_two = count % 100
    last_one = count % 10
    if 11 <= last_two <= 14:
        return "дозріло", "слів"
    if last_one == 1:
        return "дозріло", "слово"
    if 2 <= last_one <= 4:
        return "дозріли", "слова"
    return "дозріло", "слів"


def format_daily_reminder_message(due_words_count: int) -> str:
    """Format a concise, natural Ukrainian HTML reminder for due FSRS words."""
    if due_words_count <= 0:
        raise ValueError("due_words_count must be positive")
    verb, noun = _pluralize_ukrainian_words(due_words_count)
    return (
        "<b>Час для повторення німецької!</b>\n"
        f"Сьогодні на повторення {verb} <b>{due_words_count}</b> {noun}.\n"
        "Відкрий тренування командою /practice — це займе лише кілька хвилин."
    )


def _format_score_delta(delta: int) -> str:
    if delta > 0:
        return f"+{delta}"
    return str(delta)


def _format_avg_score(score: float) -> str:
    formatted = f"{score:.1f}"
    return formatted[:-2] if formatted.endswith(".0") else formatted


def format_weekly_report_message(
    radar: RadarStatsResponse,
    *,
    week_start: date,
    previous_snapshot: WeeklySnapshot | None = None,
) -> str:
    """Format a weekly progress report in Ukrainian HTML with safe escaping."""
    header = f"<b>Тижневий звіт прогресу</b> (тиждень від {week_start.strftime('%d.%m.%Y')})"
    active_axes = [axis for axis in radar.axes if axis.status == "active"]

    lines: list[str] = [header, ""]

    if previous_snapshot is not None:
        previous_scores: dict[str, int] = {
            "vocab": previous_snapshot.vocab_score,
            "grammar": previous_snapshot.grammar_score,
            "consistency": previous_snapshot.consistency_score,
        }
        lines.append("Динаміка за тиждень (було → стало):")
        for axis in active_axes:
            prev_val = previous_scores[axis.axis]
            curr_val = axis.score
            delta = _format_score_delta(curr_val - prev_val)
            label = escape_telegram_html(axis.label_ua)
            lines.append(f"• <b>{label}</b>: було {prev_val} → стало {curr_val} ({delta})")
    else:
        lines.append("Твій перший зафіксований знімок прогресу:")
        for axis in active_axes:
            label = escape_telegram_html(axis.label_ua)
            lines.append(f"• <b>{label}</b>: {axis.score} / 100")

    lines.append("")

    weak_topics = radar.weak_topics[:3]
    if weak_topics:
        lines.append("<b>Топ тем для повторення на YouTube:</b>")
        for idx, topic in enumerate(weak_topics, start=1):
            topic_name = escape_telegram_html(topic.grammar_topic)
            avg_text = _format_avg_score(topic.avg_grammar_score)
            youtube = (topic.youtube_topic or "").strip()
            if youtube:
                escaped_youtube = escape_telegram_html(youtube)
                lines.append(
                    f"{idx}. {topic_name} (середній бал: {avg_text}) — "
                    f"YouTube: «{escaped_youtube}»",
                )
            else:
                lines.append(f"{idx}. {topic_name} (середній бал: {avg_text})")
    else:
        lines.append(
            "<b>Слабких тем за останні 14 днів не виявлено</b> — чудова робота! "
            "Продовжуй у тому ж темпі.",
        )

    return "\n".join(lines)


async def run_daily_reminders(
    db: AsyncSession,
    notifier: TelegramNotifier,
    *,
    now: datetime | None = None,
) -> DailyRemindersSummary:
    """Send hourly FSRS reminders to users whose local hour matches ``reminder_hour``."""
    now_utc = datetime.now(UTC) if now is None else _ensure_utc(now, field_name="now")

    user_rows = (
        await db.execute(
            select(User.telegram_id, User.reminder_hour, User.timezone).order_by(
                User.telegram_id.asc(),
            ),
        )
    ).all()

    matched_user_ids: list[int] = []
    for row in user_rows:
        user_tz = resolve_user_timezone(row.timezone)
        local_now = now_utc.astimezone(user_tz)
        if local_now.hour == row.reminder_hour:
            matched_user_ids.append(int(row.telegram_id))

    if not matched_user_ids:
        return DailyRemindersSummary(matched_users=0, sent=0, skipped_no_due=0, failed=0)

    due_rows = (
        await db.execute(
            select(UserWordFSRS.user_id, func.count(UserWordFSRS.word_id))
            .where(
                UserWordFSRS.user_id.in_(matched_user_ids),
                UserWordFSRS.due_at <= now_utc,
            )
            .group_by(UserWordFSRS.user_id),
        )
    ).all()
    due_counts: dict[int, int] = {int(user_id): int(count) for user_id, count in due_rows}

    sent = 0
    skipped_no_due = 0
    failed = 0
    sent_user_ids: list[int] = []
    failed_user_ids: list[int] = []

    for user_id in matched_user_ids:
        due_count = due_counts.get(user_id, 0)
        if due_count <= 0:
            skipped_no_due += 1
            continue

        message = format_daily_reminder_message(due_count)
        try:
            await notifier.send_message(
                chat_id=user_id,
                text=message,
                parse_mode="HTML",
                is_html=True,
            )
        except TelegramDeliveryError as exc:
            failed += 1
            failed_user_ids.append(user_id)
            logger.warning(
                "Failed to send daily reminder to chat_id=%s status=%s reason=%s retry_after=%s",
                user_id,
                exc.status_code,
                exc.reason,
                exc.retry_after,
            )
        except Exception as exc:
            failed += 1
            failed_user_ids.append(user_id)
            logger.error(
                "Unexpected error sending daily reminder to chat_id=%s error=%s",
                user_id,
                type(exc).__name__,
            )
        else:
            sent += 1
            sent_user_ids.append(user_id)

    return DailyRemindersSummary(
        matched_users=len(matched_user_ids),
        sent=sent,
        skipped_no_due=skipped_no_due,
        failed=failed,
        sent_user_ids=tuple(sent_user_ids),
        failed_user_ids=tuple(failed_user_ids),
    )


async def run_weekly_reports(
    db: AsyncSession,
    notifier: TelegramNotifier,
    *,
    now: datetime | None = None,
) -> WeeklyReportsSummary:
    """Persist idempotent weekly radar snapshots and send Ukrainian progress reports."""
    now_utc = datetime.now(UTC) if now is None else _ensure_utc(now, field_name="now")
    week_start = get_week_start_utc(now_utc)

    user_ids = [
        int(uid)
        for uid in (
            await db.execute(select(User.telegram_id).order_by(User.telegram_id.asc()))
        ).scalars()
    ]

    if not user_ids:
        return WeeklyReportsSummary(
            week_start=week_start,
            total_users=0,
            snapshots_created=0,
            sent=0,
            skipped_existing=0,
            failed=0,
        )

    existing_user_ids = {
        int(uid)
        for uid in (
            await db.execute(
                select(WeeklySnapshot.user_id).where(WeeklySnapshot.week_start == week_start),
            )
        ).scalars()
    }

    snapshots_created = 0
    sent = 0
    skipped_existing = 0
    failed = 0
    sent_user_ids: list[int] = []
    failed_user_ids: list[int] = []

    for user_id in user_ids:
        if user_id in existing_user_ids:
            skipped_existing += 1
            continue

        try:
            radar = await compute_user_radar(db, user_id, now=now_utc)
            previous_snapshot = (
                await db.execute(
                    select(WeeklySnapshot)
                    .where(
                        WeeklySnapshot.user_id == user_id,
                        WeeklySnapshot.week_start < week_start,
                    )
                    .order_by(WeeklySnapshot.week_start.desc())
                    .limit(1),
                )
            ).scalar_one_or_none()

            scores = radar.scores_by_axis()
            async with db.begin_nested():
                insert_stmt = (
                    pg_insert(WeeklySnapshot)
                    .values(
                        user_id=user_id,
                        week_start=week_start,
                        vocab_score=scores["vocab"],
                        grammar_score=scores["grammar"],
                        consistency_score=scores["consistency"],
                        writing_score=scores["writing"],
                        listening_score=scores["listening"],
                    )
                    .on_conflict_do_nothing(index_elements=["user_id", "week_start"])
                    .returning(WeeklySnapshot.id)
                )
                inserted_id = (await db.execute(insert_stmt)).scalar_one_or_none()

            if inserted_id is None:
                skipped_existing += 1
                continue

            await db.commit()
            snapshots_created += 1

            message = format_weekly_report_message(
                radar,
                week_start=week_start,
                previous_snapshot=previous_snapshot,
            )

            try:
                await notifier.send_message(
                    chat_id=user_id,
                    text=message,
                    parse_mode="HTML",
                    is_html=True,
                )
            except TelegramDeliveryError as exc:
                failed += 1
                failed_user_ids.append(user_id)
                logger.warning(
                    "Failed to send weekly report to chat_id=%s status=%s reason=%s retry_after=%s",
                    user_id,
                    exc.status_code,
                    exc.reason,
                    exc.retry_after,
                )
            else:
                sent += 1
                sent_user_ids.append(user_id)
        except Exception as exc:
            failed += 1
            failed_user_ids.append(user_id)
            logger.error(
                "Unexpected error processing weekly report for user_id=%s error=%s",
                user_id,
                type(exc).__name__,
            )

    return WeeklyReportsSummary(
        week_start=week_start,
        total_users=len(user_ids),
        snapshots_created=snapshots_created,
        sent=sent,
        skipped_existing=skipped_existing,
        failed=failed,
        sent_user_ids=tuple(sent_user_ids),
        failed_user_ids=tuple(failed_user_ids),
    )


__all__ = [
    "DailyRemindersSummary",
    "WeeklyReportsSummary",
    "format_daily_reminder_message",
    "format_weekly_report_message",
    "get_week_start_utc",
    "resolve_user_timezone",
    "run_daily_reminders",
    "run_weekly_reports",
]
