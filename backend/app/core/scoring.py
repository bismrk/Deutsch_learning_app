"""B1-calibrated 5-axis radar scoring formulas and async statistics service."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Attempt, User, UserWordFSRS
from app.schemas.stats import (
    RADAR_AXIS_LABELS_UA,
    RadarAxis,
    RadarStatsResponse,
    WeakTopicItem,
)

VOCAB_STABILITY_THRESHOLD_DAYS = 21.0
VOCAB_B1_TARGET_WORDS = 1500.0
VOCAB_RECALL_WINDOW_DAYS = 30
VOCAB_GOOD_RATING_MIN = 3
FSRS_RATING_MIN = 1
FSRS_RATING_MAX = 4

GRAMMAR_ATTEMPTS_LIMIT = 40
GRAMMAR_FULL_COVERAGE_TOPICS = 6.0
LEVEL_WEIGHTS: dict[str, float] = {
    "A1": 0.35,
    "A2": 0.70,
    "B1": 1.0,
}

CONSISTENCY_WINDOW_DAYS = 14
CONSISTENCY_TARGET_ACTIVE_DAYS = 12.0
OVERDUE_GRACE_HOURS = 24

WEAK_TOPICS_WINDOW_DAYS = 14
WEAK_TOPICS_MAX_ITEMS = 3
WEAK_TOPICS_SCORE_CEILING = 100.0

DEFAULT_TIMEZONE = "Europe/Kyiv"


@dataclass(frozen=True, slots=True)
class GrammarAttemptInput:
    """Minimal completed attempt projection required for ``grammar_score`` calculation."""

    grammar_score: int
    level: str
    grammar_topic: str


@dataclass(frozen=True, slots=True)
class WeakTopicAttemptInput:
    """Minimal completed attempt projection required for 14-day weak topic aggregation."""

    grammar_topic: str
    grammar_score: int
    youtube_topic: str | None = None
    created_at: datetime | None = None
    attempt_id: int = 0


def _ensure_utc(value: datetime, *, field_name: str = "datetime") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _clamp_score(value: int) -> int:
    return max(0, min(100, value))


def _resolve_zoneinfo(timezone_name: str | None) -> tzinfo:
    candidate = (timezone_name or "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        try:
            return ZoneInfo(DEFAULT_TIMEZONE)
        except ZoneInfoNotFoundError:
            return UTC


def _is_valid_fsrs_rating(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and FSRS_RATING_MIN <= value <= FSRS_RATING_MAX
    )


def _extract_rating_from_item(item: Any) -> int | None:
    if _is_valid_fsrs_rating(item):
        return int(item)
    if isinstance(item, dict):
        raw = item.get("fsrs_rating")
        if raw is None:
            raw = item.get("rating")
        if _is_valid_fsrs_rating(raw):
            return int(raw)
    return None


def extract_fsrs_rating_counts(fsrs_ratings_entries: Sequence[Any]) -> tuple[int, int]:
    """Extract ``(good_or_easy_count, total_reviews)`` from Attempt ``fsrs_ratings`` JSONB values.

    Supports both the canonical list-of-dicts format produced by training sessions
    (``[{"word_id": 1, "fsrs_rating": 3, ...}]``) and dictionary mappings
    (``{"1": 3}`` or ``{"1": {"fsrs_rating": 3}}``).
    """
    good_or_easy = 0
    total_reviews = 0

    for entry in fsrs_ratings_entries:
        if entry is None:
            continue

        ratings_to_check: list[int] = []
        if isinstance(entry, list):
            for item in entry:
                parsed = _extract_rating_from_item(item)
                if parsed is not None:
                    ratings_to_check.append(parsed)
        elif isinstance(entry, dict):
            direct = _extract_rating_from_item(entry)
            if direct is not None:
                ratings_to_check.append(direct)
            else:
                for value in entry.values():
                    parsed = _extract_rating_from_item(value)
                    if parsed is not None:
                        ratings_to_check.append(parsed)

        for rating in ratings_to_check:
            total_reviews += 1
            if rating >= VOCAB_GOOD_RATING_MIN:
                good_or_easy += 1

    return good_or_easy, total_reviews


def calculate_vocab_score(
    card_stabilities: Sequence[float | None],
    *,
    good_or_easy_count_30d: int = 0,
    total_reviews_30d: int = 0,
) -> int:
    """Compute ``vocab_score`` (0–100) per ``_docs/plan.md`` section 6.1.

    - ``effective_words = sum(min(1.0, max(0.0, stability or 0.0) / 21.0))``
    - ``volume_part = min(1.0, effective_words / 1500.0) ** 0.5``
    - ``recall_part = good_or_easy_count_30d / max(1, total_reviews_30d)``
    - ``vocab_score = round(100 * (0.75 * volume_part + 0.25 * recall_part))``
    """
    effective_words = sum(
        min(1.0, max(0.0, float(stability)) / VOCAB_STABILITY_THRESHOLD_DAYS)
        for stability in card_stabilities
        if stability is not None
    )
    volume_part = min(1.0, effective_words / VOCAB_B1_TARGET_WORDS) ** 0.5

    safe_total = max(0, total_reviews_30d)
    safe_good = max(0, min(good_or_easy_count_30d, safe_total))
    recall_part = safe_good / max(1, safe_total)

    return _clamp_score(round(100 * (0.75 * volume_part + 0.25 * recall_part)))


def calculate_grammar_score(attempts: Sequence[GrammarAttemptInput]) -> int:
    """Compute ``grammar_score`` (0–100) per ``_docs/plan.md`` section 6.2.

    Uses at most the 40 most recent completed attempts, applies CEFR level weights
    (``A1=0.35``, ``A2=0.70``, ``B1=1.0``), and applies the topic coverage multiplier
    ``0.7 + 0.3 * min(1.0, unique_topics / 6.0)``.
    """
    recent = attempts[:GRAMMAR_ATTEMPTS_LIMIT]
    if not recent:
        return 0

    weighted_scores = [
        attempt.grammar_score * LEVEL_WEIGHTS.get(attempt.level, 0.0)
        for attempt in recent
    ]
    base_grammar = sum(weighted_scores) / max(1, len(weighted_scores))

    unique_topics = len(
        {
            attempt.grammar_topic.strip()
            for attempt in recent
            if attempt.grammar_topic and attempt.grammar_topic.strip()
        },
    )
    coverage_factor = min(1.0, unique_topics / GRAMMAR_FULL_COVERAGE_TOPICS)

    return _clamp_score(round(base_grammar * (0.7 + 0.3 * coverage_factor)))


def count_active_days(
    attempt_timestamps: Sequence[datetime],
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
    now: datetime,
    window_days: int = CONSISTENCY_WINDOW_DAYS,
) -> int:
    """Count distinct local calendar days with attempts in ``[now - window_days, now]``."""
    now_utc = _ensure_utc(now, field_name="now")
    cutoff_utc = now_utc - timedelta(days=window_days)
    target_tz = _resolve_zoneinfo(timezone_name)

    active_dates: set[date] = set()
    for timestamp in attempt_timestamps:
        ts_utc = _ensure_utc(timestamp, field_name="attempt_timestamp")
        if cutoff_utc <= ts_utc <= now_utc:
            active_dates.add(ts_utc.astimezone(target_tz).date())

    return len(active_dates)


def count_overdue_active_cards(
    cards: Sequence[tuple[int, datetime]],
    *,
    now: datetime,
    grace_hours: int = OVERDUE_GRACE_HOURS,
) -> tuple[int, int]:
    """Count ``(overdue_words_24h, total_active_words)`` among reviewed cards (``reps > 0``).

    A card is considered overdue only when ``due_at < now - timedelta(hours=grace_hours)``
    (strictly more than ``grace_hours`` past due; ``<= 24h`` carries no penalty).
    """
    now_utc = _ensure_utc(now, field_name="now")
    overdue_cutoff = now_utc - timedelta(hours=grace_hours)

    total_active_words = 0
    overdue_words_24h = 0

    for reps, due_at in cards:
        if reps <= 0:
            continue
        total_active_words += 1
        if _ensure_utc(due_at, field_name="due_at") < overdue_cutoff:
            overdue_words_24h += 1

    return overdue_words_24h, total_active_words


def calculate_consistency_score(
    *,
    active_days_last_14d: int,
    overdue_words_24h: int,
    total_active_words: int,
) -> int:
    """Compute ``consistency_score`` (0–100) per ``_docs/plan.md`` section 6.3.

    When ``total_active_words == 0``, ``backlog_health`` is ``0.0`` so a brand-new user
    with an empty database starts at ``0`` instead of ``40``.
    """
    safe_days = max(0, active_days_last_14d)
    days_ratio = min(1.0, safe_days / CONSISTENCY_TARGET_ACTIVE_DAYS)

    if total_active_words <= 0:
        backlog_health = 0.0
    else:
        safe_overdue = max(0, overdue_words_24h)
        overdue_ratio = safe_overdue / max(1, total_active_words)
        backlog_health = max(0.0, 1.0 - overdue_ratio)

    return _clamp_score(round(100 * (0.6 * days_ratio + 0.4 * backlog_health)))


def aggregate_weak_topics(
    attempts: Sequence[WeakTopicAttemptInput],
    *,
    limit: int = WEAK_TOPICS_MAX_ITEMS,
) -> list[WeakTopicItem]:
    """Aggregate up to ``limit`` weakest grammar topics (``avg_grammar_score < 100``).

    Sorted by ascending average grammar score, then descending attempt count on ties,
    then case-insensitive topic name for determinism. Selects the most recent non-empty
    ``youtube_topic`` within each topic group.
    """
    grouped: dict[str, list[WeakTopicAttemptInput]] = {}
    for attempt in attempts:
        topic = attempt.grammar_topic.strip()
        if not topic:
            continue
        grouped.setdefault(topic, []).append(attempt)

    candidates: list[tuple[float, int, str, WeakTopicItem]] = []
    for topic, topic_attempts in grouped.items():
        attempts_count = len(topic_attempts)
        raw_avg = sum(item.grammar_score for item in topic_attempts) / attempts_count
        if raw_avg >= WEAK_TOPICS_SCORE_CEILING:
            continue

        chosen_youtube: str | None = None
        if any(item.created_at is not None for item in topic_attempts):
            ordered_for_youtube = sorted(
                topic_attempts,
                key=lambda item: (
                    _ensure_utc(item.created_at, field_name="created_at")
                    if item.created_at is not None
                    else datetime.min.replace(tzinfo=UTC),
                    item.attempt_id,
                ),
                reverse=True,
            )
        else:
            ordered_for_youtube = topic_attempts

        for item in ordered_for_youtube:
            if item.youtube_topic and item.youtube_topic.strip():
                chosen_youtube = item.youtube_topic.strip()
                break

        avg_display = min(99.9, round(raw_avg, 1))
        candidates.append(
            (
                raw_avg,
                -attempts_count,
                topic.casefold(),
                WeakTopicItem(
                    grammar_topic=topic,
                    avg_grammar_score=avg_display,
                    attempts_count=attempts_count,
                    youtube_topic=chosen_youtube,
                ),
            ),
        )

    candidates.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    return [entry[3] for entry in candidates[: max(0, limit)]]


def build_radar_axes(
    *,
    vocab_score: int,
    grammar_score: int,
    consistency_score: int,
) -> list[RadarAxis]:
    """Assemble the 5 radar axes in fixed canonical order with MVP coming-soon stubs."""
    return [
        RadarAxis(
            axis="vocab",
            label_ua=RADAR_AXIS_LABELS_UA["vocab"],
            score=_clamp_score(vocab_score),
            status="active",
        ),
        RadarAxis(
            axis="grammar",
            label_ua=RADAR_AXIS_LABELS_UA["grammar"],
            score=_clamp_score(grammar_score),
            status="active",
        ),
        RadarAxis(
            axis="consistency",
            label_ua=RADAR_AXIS_LABELS_UA["consistency"],
            score=_clamp_score(consistency_score),
            status="active",
        ),
        RadarAxis(
            axis="writing",
            label_ua=RADAR_AXIS_LABELS_UA["writing"],
            score=0,
            status="coming_soon",
        ),
        RadarAxis(
            axis="listening",
            label_ua=RADAR_AXIS_LABELS_UA["listening"],
            score=0,
            status="coming_soon",
        ),
    ]


async def compute_user_radar(
    db: AsyncSession,
    user_id: int,
    now: datetime | None = None,
) -> RadarStatsResponse:
    """Compute the 5-axis radar chart and top-3 weak topics strictly for ``user_id``.

    Read-only async service suitable for both ``GET /api/stats/radar`` and the weekly
    scheduler snapshot job. Accepts a deterministic timezone-aware UTC ``now`` parameter.
    """
    now_utc = datetime.now(UTC) if now is None else _ensure_utc(now, field_name="now")
    cutoff_30d = now_utc - timedelta(days=VOCAB_RECALL_WINDOW_DAYS)
    cutoff_14d = now_utc - timedelta(days=CONSISTENCY_WINDOW_DAYS)

    timezone_name = (
        await db.execute(select(User.timezone).where(User.telegram_id == user_id))
    ).scalar_one_or_none() or DEFAULT_TIMEZONE

    card_rows = (
        await db.execute(
            select(
                UserWordFSRS.stability,
                UserWordFSRS.reps,
                UserWordFSRS.due_at,
            ).where(UserWordFSRS.user_id == user_id),
        )
    ).all()

    card_stabilities = [row.stability for row in card_rows]
    overdue_words_24h, total_active_words = count_overdue_active_cards(
        [(row.reps, row.due_at) for row in card_rows],
        now=now_utc,
    )

    grammar_rows = (
        await db.execute(
            select(
                Attempt.grammar_score,
                Attempt.level,
                Attempt.grammar_topic,
            )
            .where(
                Attempt.user_id == user_id,
                Attempt.user_answer.is_not(None),
                Attempt.grammar_score.is_not(None),
                Attempt.created_at <= now_utc,
            )
            .order_by(Attempt.created_at.desc(), Attempt.id.desc())
            .limit(GRAMMAR_ATTEMPTS_LIMIT),
        )
    ).all()

    grammar_inputs = [
        GrammarAttemptInput(
            grammar_score=row.grammar_score,
            level=row.level,
            grammar_topic=row.grammar_topic,
        )
        for row in grammar_rows
        if row.grammar_score is not None
    ]
    grammar_score = calculate_grammar_score(grammar_inputs)

    recent_rows = (
        await db.execute(
            select(
                Attempt.id,
                Attempt.created_at,
                Attempt.grammar_topic,
                Attempt.grammar_score,
                Attempt.fsrs_ratings,
                Attempt.youtube_topic,
            )
            .where(
                Attempt.user_id == user_id,
                Attempt.user_answer.is_not(None),
                Attempt.created_at >= cutoff_30d,
                Attempt.created_at <= now_utc,
            )
            .order_by(Attempt.created_at.desc(), Attempt.id.desc()),
        )
    ).all()

    good_or_easy_30d, total_reviews_30d = extract_fsrs_rating_counts(
        [row.fsrs_ratings for row in recent_rows],
    )
    vocab_score = calculate_vocab_score(
        card_stabilities,
        good_or_easy_count_30d=good_or_easy_30d,
        total_reviews_30d=total_reviews_30d,
    )

    rows_14d = [row for row in recent_rows if _ensure_utc(row.created_at) >= cutoff_14d]
    active_days_14d = count_active_days(
        [row.created_at for row in rows_14d],
        timezone_name=timezone_name,
        now=now_utc,
    )
    consistency_score = calculate_consistency_score(
        active_days_last_14d=active_days_14d,
        overdue_words_24h=overdue_words_24h,
        total_active_words=total_active_words,
    )

    weak_topic_inputs = [
        WeakTopicAttemptInput(
            grammar_topic=row.grammar_topic,
            grammar_score=row.grammar_score,
            youtube_topic=row.youtube_topic,
            created_at=row.created_at,
            attempt_id=row.id,
        )
        for row in rows_14d
        if row.grammar_score is not None
    ]
    weak_topics = aggregate_weak_topics(weak_topic_inputs)

    return RadarStatsResponse(
        axes=build_radar_axes(
            vocab_score=vocab_score,
            grammar_score=grammar_score,
            consistency_score=consistency_score,
        ),
        weak_topics=weak_topics,
    )


__all__ = [
    "CONSISTENCY_TARGET_ACTIVE_DAYS",
    "CONSISTENCY_WINDOW_DAYS",
    "DEFAULT_TIMEZONE",
    "GRAMMAR_ATTEMPTS_LIMIT",
    "GRAMMAR_FULL_COVERAGE_TOPICS",
    "LEVEL_WEIGHTS",
    "OVERDUE_GRACE_HOURS",
    "VOCAB_B1_TARGET_WORDS",
    "VOCAB_GOOD_RATING_MIN",
    "VOCAB_RECALL_WINDOW_DAYS",
    "VOCAB_STABILITY_THRESHOLD_DAYS",
    "WEAK_TOPICS_MAX_ITEMS",
    "WEAK_TOPICS_SCORE_CEILING",
    "WEAK_TOPICS_WINDOW_DAYS",
    "GrammarAttemptInput",
    "WeakTopicAttemptInput",
    "aggregate_weak_topics",
    "build_radar_axes",
    "calculate_consistency_score",
    "calculate_grammar_score",
    "calculate_vocab_score",
    "compute_user_radar",
    "count_active_days",
    "count_overdue_active_cards",
    "extract_fsrs_rating_counts",
]
