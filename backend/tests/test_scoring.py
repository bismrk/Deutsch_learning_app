"""Unit and PostgreSQL service tests for 5-axis radar scoring formulas."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scoring import (
    GrammarAttemptInput,
    WeakTopicAttemptInput,
    aggregate_weak_topics,
    build_radar_axes,
    calculate_consistency_score,
    calculate_grammar_score,
    calculate_vocab_score,
    compute_user_radar,
    count_active_days,
    count_overdue_active_cards,
    extract_fsrs_rating_counts,
)
from app.db.models import Attempt, Deck, TrainingSession, User, UserWordFSRS, Word
from app.schemas.stats import RADAR_AXIS_LABELS_UA, RADAR_AXIS_ORDER

FIXED_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
USER_SCORING_ID = 6_100_000_001


# ---------------------------------------------------------------------------
# Pure unit tests: vocab_score & FSRS rating extraction
# ---------------------------------------------------------------------------


def test_vocab_score_empty_returns_zero() -> None:
    assert calculate_vocab_score([], good_or_easy_count_30d=0, total_reviews_30d=0) == 0


def test_vocab_score_handles_none_zero_and_caps_stability_at_21_days() -> None:
    # None and 0.0 contribute 0 effective words
    assert (
        calculate_vocab_score([None, None, 0.0], good_or_easy_count_30d=0, total_reviews_30d=0)
        == 0
    )

    # Stability > 21 days is capped at 1.0 effective word (same result as 21.0)
    score_at_21 = calculate_vocab_score(
        [21.0] * 375,
        good_or_easy_count_30d=10,
        total_reviews_30d=10,
    )
    score_above_21 = calculate_vocab_score(
        [45.0] * 375,
        good_or_easy_count_30d=10,
        total_reviews_30d=10,
    )
    # effective_words = 375 -> volume_part = (375/1500)**0.5 = 0.5; recall_part = 1.0
    # 100 * (0.75 * 0.5 + 0.25 * 1.0) = 62.5 -> round(62.5) = 62
    assert score_at_21 == 62
    assert score_above_21 == score_at_21

    # Partial stability (10.5 days = 0.5 effective words each) + None mixed in
    # 750 cards * 0.5 = 375 effective words -> volume_part = 0.5; recall_part = 0.0
    # 100 * (0.75 * 0.5) = 37.5 -> round(37.5) = 38
    assert (
        calculate_vocab_score(
            [10.5] * 750 + [None] * 25,
            good_or_easy_count_30d=0,
            total_reviews_30d=0,
        )
        == 38
    )


def test_vocab_score_reaches_100_at_b1_benchmark_and_caps() -> None:
    assert (
        calculate_vocab_score(
            [21.0] * 1500,
            good_or_easy_count_30d=20,
            total_reviews_30d=20,
        )
        == 100
    )
    # Above 1500 mastered words still capped at 100
    assert (
        calculate_vocab_score(
            [60.0] * 2000,
            good_or_easy_count_30d=50,
            total_reviews_30d=50,
        )
        == 100
    )
    # 1500 words but 0% good/easy recall -> 75
    assert (
        calculate_vocab_score(
            [21.0] * 1500,
            good_or_easy_count_30d=0,
            total_reviews_30d=10,
        )
        == 75
    )


def test_extract_fsrs_rating_counts_supports_list_and_dict_shapes() -> None:
    entries = [
        None,
        [],
        [
            {"word_id": 1, "word_de": "der Zug", "fsrs_rating": 1, "note_ua": "Again"},
            {"word_id": 2, "word_de": "das Haus", "fsrs_rating": 2, "note_ua": "Hard"},
            {"word_id": 3, "word_de": "die Zeit", "fsrs_rating": 3, "note_ua": "Good"},
            {"word_id": 4, "word_de": "die Arbeit", "fsrs_rating": 4, "note_ua": "Easy"},
            {"word_id": 5, "fsrs_rating": True},  # bool ignored
            {"word_id": 6, "fsrs_rating": 99},  # out of range ignored
        ],
        {"10": 3, "11": 2, "12": {"fsrs_rating": 4}},
    ]
    good_or_easy, total = extract_fsrs_rating_counts(entries)
    assert (good_or_easy, total) == (4, 7)


# ---------------------------------------------------------------------------
# Pure unit tests: grammar_score
# ---------------------------------------------------------------------------


def test_grammar_score_empty_returns_zero() -> None:
    assert calculate_grammar_score([]) == 0


@pytest.mark.parametrize(
    ("level", "expected_max"),
    [
        ("A1", 35),
        ("A2", 70),
        ("B1", 100),
    ],
)
def test_grammar_score_applies_cefr_level_weights_with_full_topic_coverage(
    level: str,
    expected_max: int,
) -> None:
    attempts = [
        GrammarAttemptInput(grammar_score=100, level=level, grammar_topic=f"Topic {idx}")
        for idx in range(6)
    ]
    assert calculate_grammar_score(attempts) == expected_max


def test_grammar_score_single_topic_grinding_limited_by_coverage_factor() -> None:
    # 1 unique topic -> coverage_factor = 1/6 -> multiplier = 0.7 + 0.3*(1/6) = 0.75
    b1_single = [
        GrammarAttemptInput(grammar_score=100, level="B1", grammar_topic="Nebensätze mit weil")
        for _ in range(10)
    ]
    assert calculate_grammar_score(b1_single) == 75

    a2_single = [
        GrammarAttemptInput(grammar_score=100, level="A2", grammar_topic="Perfekt")
        for _ in range(10)
    ]
    assert calculate_grammar_score(a2_single) == round(70.0 * 0.75)
    assert calculate_grammar_score(a2_single) <= 70

    a1_single = [
        GrammarAttemptInput(grammar_score=100, level="A1", grammar_topic="Präsens")
        for _ in range(10)
    ]
    assert calculate_grammar_score(a1_single) == round(35.0 * 0.75)
    assert calculate_grammar_score(a1_single) <= 35

    # 3 unique topics -> coverage_factor = 0.5 -> multiplier = 0.85
    b1_three_topics = [
        GrammarAttemptInput(grammar_score=100, level="B1", grammar_topic=f"Topic {idx % 3}")
        for idx in range(6)
    ]
    assert calculate_grammar_score(b1_three_topics) == 85


def test_grammar_score_uses_at_most_40_most_recent_attempts() -> None:
    recent_40 = [
        GrammarAttemptInput(grammar_score=100, level="B1", grammar_topic=f"Topic {idx % 6}")
        for idx in range(40)
    ]
    older_10 = [
        GrammarAttemptInput(grammar_score=0, level="A1", grammar_topic="Old Topic")
        for _ in range(10)
    ]
    assert calculate_grammar_score([*recent_40, *older_10]) == 100

    recent_40_zero = [
        GrammarAttemptInput(grammar_score=0, level="B1", grammar_topic=f"Topic {idx % 6}")
        for idx in range(40)
    ]
    older_10_perfect = [
        GrammarAttemptInput(grammar_score=100, level="B1", grammar_topic=f"Old {idx}")
        for idx in range(10)
    ]
    assert calculate_grammar_score([*recent_40_zero, *older_10_perfect]) == 0


# ---------------------------------------------------------------------------
# Pure unit tests: consistency_score, timezone boundaries & 24h overdue rule
# ---------------------------------------------------------------------------


def test_consistency_score_empty_db_is_zero_not_40() -> None:
    assert (
        calculate_consistency_score(
            active_days_last_14d=0,
            overdue_words_24h=0,
            total_active_words=0,
        )
        == 0
    )


def test_consistency_score_regularity_and_overdue_penalties() -> None:
    # 0 active days, 5 active non-overdue words -> 0.4 * 1.0 = 40
    assert (
        calculate_consistency_score(
            active_days_last_14d=0,
            overdue_words_24h=0,
            total_active_words=5,
        )
        == 40
    )
    # 12 active days, 0 overdue active words -> 100
    assert (
        calculate_consistency_score(
            active_days_last_14d=12,
            overdue_words_24h=0,
            total_active_words=10,
        )
        == 100
    )
    # 14 active days capped at 12 -> 100
    assert (
        calculate_consistency_score(
            active_days_last_14d=14,
            overdue_words_24h=0,
            total_active_words=10,
        )
        == 100
    )
    # 12 active days, 100% overdue -> 60
    assert (
        calculate_consistency_score(
            active_days_last_14d=12,
            overdue_words_24h=10,
            total_active_words=10,
        )
        == 60
    )
    # 6 active days (0.5), 50% overdue (0.5) -> 50
    assert (
        calculate_consistency_score(
            active_days_last_14d=6,
            overdue_words_24h=5,
            total_active_words=10,
        )
        == 50
    )


def test_count_active_days_respects_user_timezone_midnight_boundary() -> None:
    # In May, Europe/Kyiv is UTC+3:
    # 2026-05-10T20:30Z -> 2026-05-10 23:30+03 (May 10)
    # 2026-05-10T21:30Z -> 2026-05-11 00:30+03 (May 11)
    same_utc_day_timestamps = [
        datetime(2026, 5, 10, 20, 30, 0, tzinfo=UTC),
        datetime(2026, 5, 10, 21, 30, 0, tzinfo=UTC),
    ]
    assert count_active_days(same_utc_day_timestamps, timezone_name="UTC", now=FIXED_NOW) == 1
    assert (
        count_active_days(same_utc_day_timestamps, timezone_name="Europe/Kyiv", now=FIXED_NOW) == 2
    )

    # Across UTC midnight, both fall on the same local calendar day in Kyiv and New York:
    # 2026-05-10T23:30Z -> 2026-05-11 02:30+03 (Kyiv) / 2026-05-10 19:30-04 (NY)
    # 2026-05-11T00:30Z -> 2026-05-11 03:30+03 (Kyiv) / 2026-05-10 20:30-04 (NY)
    across_utc_midnight = [
        datetime(2026, 5, 10, 23, 30, 0, tzinfo=UTC),
        datetime(2026, 5, 11, 0, 30, 0, tzinfo=UTC),
    ]
    assert count_active_days(across_utc_midnight, timezone_name="UTC", now=FIXED_NOW) == 2
    assert count_active_days(across_utc_midnight, timezone_name="Europe/Kyiv", now=FIXED_NOW) == 1
    assert (
        count_active_days(across_utc_midnight, timezone_name="America/New_York", now=FIXED_NOW)
        == 1
    )


def test_count_active_days_excludes_older_than_14_days_and_future() -> None:
    timestamps = [
        FIXED_NOW - timedelta(days=14, seconds=1),  # excluded (older than 14d)
        FIXED_NOW - timedelta(days=14),  # included (exact boundary)
        FIXED_NOW - timedelta(days=2),  # included
        FIXED_NOW + timedelta(seconds=1),  # excluded (future)
    ]
    assert count_active_days(timestamps, timezone_name="UTC", now=FIXED_NOW) == 2

    with pytest.raises(ValueError, match="timezone-aware"):
        count_active_days([FIXED_NOW], timezone_name="UTC", now=datetime(2026, 5, 15, 12, 0))


def test_count_overdue_active_cards_24h_boundary_and_unreviewed_exclusion() -> None:
    cards = [
        (0, FIXED_NOW - timedelta(days=10)),  # reps == 0 (new) -> excluded
        (1, FIXED_NOW + timedelta(days=2)),  # upcoming -> active, not overdue
        (2, FIXED_NOW - timedelta(hours=23, minutes=59)),  # <= 24h -> active, not overdue
        (3, FIXED_NOW - timedelta(hours=24)),  # exact 24h (<= 24h) -> active, not overdue
        (4, FIXED_NOW - timedelta(hours=24, seconds=1)),  # > 24h -> active, OVERDUE
        (5, FIXED_NOW - timedelta(days=5)),  # > 24h -> active, OVERDUE
    ]
    overdue, total_active = count_overdue_active_cards(cards, now=FIXED_NOW)
    assert (overdue, total_active) == (2, 5)


# ---------------------------------------------------------------------------
# Pure unit tests: weak_topics & radar axes assembly
# ---------------------------------------------------------------------------


def test_aggregate_weak_topics_ordering_ties_limit_and_perfect_exclusion() -> None:
    assert aggregate_weak_topics([]) == []

    perfect_only = [
        WeakTopicAttemptInput(
            grammar_topic="Präsens",
            grammar_score=100,
            youtube_topic="Präsens A1",
            created_at=FIXED_NOW - timedelta(days=1),
            attempt_id=1,
        ),
    ]
    assert aggregate_weak_topics(perfect_only) == []

    attempts = [
        # Perfect topic (avg 100) -> excluded
        WeakTopicAttemptInput("Präsens", 100, "Präsens A1", FIXED_NOW - timedelta(days=1), 1),
        # Topic A: avg 60.0, 2 attempts (most recent youtube_topic must win)
        WeakTopicAttemptInput("Akkusativ", 50, "Old Akkusativ", FIXED_NOW - timedelta(days=4), 2),
        WeakTopicAttemptInput(
            "Akkusativ", 70, "Новий Akkusativ A2", FIXED_NOW - timedelta(days=1), 3
        ),
        # Topic B: avg 60.0, 3 attempts (same avg as A, more attempts -> ranks BEFORE A)
        WeakTopicAttemptInput("Dativ", 60, "Dativ відео 1", FIXED_NOW - timedelta(days=5), 4),
        WeakTopicAttemptInput("Dativ", 60, None, FIXED_NOW - timedelta(days=2), 5),
        WeakTopicAttemptInput("Dativ", 60, "Dativ відео 2", FIXED_NOW - timedelta(days=3), 6),
        # Topic C: avg 40.0, 1 attempt (weakest -> ranks #1)
        WeakTopicAttemptInput(
            "Nebensätze mit weil", 40, "Порядок слів weil B1", FIXED_NOW - timedelta(days=1), 7
        ),
        # Topic D: avg 75.0, 1 attempt (4th weakest -> cut off by top 3)
        WeakTopicAttemptInput("Perfekt", 75, "Perfekt A2", FIXED_NOW - timedelta(days=1), 8),
    ]
    weak = aggregate_weak_topics(attempts)
    assert len(weak) == 3

    assert weak[0].grammar_topic == "Nebensätze mit weil"
    assert weak[0].avg_grammar_score == 40.0
    assert weak[0].attempts_count == 1
    assert weak[0].youtube_topic == "Порядок слів weil B1"

    assert weak[1].grammar_topic == "Dativ"
    assert weak[1].avg_grammar_score == 60.0
    assert weak[1].attempts_count == 3
    assert weak[1].youtube_topic == "Dativ відео 2"

    assert weak[2].grammar_topic == "Akkusativ"
    assert weak[2].avg_grammar_score == 60.0
    assert weak[2].attempts_count == 2
    assert weak[2].youtube_topic == "Новий Akkusativ A2"


def test_build_radar_axes_canonical_order_labels_and_coming_soon_stubs() -> None:
    axes = build_radar_axes(vocab_score=55, grammar_score=70, consistency_score=88)
    assert len(axes) == 5
    assert tuple(item.axis for item in axes) == RADAR_AXIS_ORDER
    for item in axes:
        assert item.label_ua == RADAR_AXIS_LABELS_UA[item.axis]
        assert 0 <= item.score <= 100

    assert axes[0].score == 55
    assert axes[0].status == "active"
    assert axes[1].score == 70
    assert axes[1].status == "active"
    assert axes[2].score == 88
    assert axes[2].status == "active"
    assert axes[3].axis == "writing"
    assert axes[3].score == 0
    assert axes[3].status == "coming_soon"
    assert axes[4].axis == "listening"
    assert axes[4].score == 0
    assert axes[4].status == "coming_soon"


# ---------------------------------------------------------------------------
# PostgreSQL integration tests for compute_user_radar
# ---------------------------------------------------------------------------


async def test_compute_user_radar_empty_database_returns_zeros(
    db_session: AsyncSession,
) -> None:
    radar = await compute_user_radar(db_session, USER_SCORING_ID, now=FIXED_NOW)
    assert tuple(axis.axis for axis in radar.axes) == RADAR_AXIS_ORDER
    assert radar.scores_by_axis() == {
        "vocab": 0,
        "grammar": 0,
        "consistency": 0,
        "writing": 0,
        "listening": 0,
    }
    assert radar.weak_topics == []


async def test_compute_user_radar_enforces_40_attempt_limit_30d_and_14d_windows(
    db_session: AsyncSession,
) -> None:
    user = User(telegram_id=USER_SCORING_ID, level="B1", timezone="UTC")
    db_session.add(user)
    await db_session.flush()

    deck = Deck(owner_id=USER_SCORING_ID, title="Мій словник", level="mixed")
    db_session.add(deck)
    await db_session.flush()

    word = Word(deck_id=deck.id, word_de="der Zug", translation_ua="потяг", source="text")
    db_session.add(word)
    await db_session.flush()

    # 1 active reviewed card, overdue by exactly 24h (<= 24h -> NO overdue penalty!)
    db_session.add(
        UserWordFSRS(
            user_id=USER_SCORING_ID,
            word_id=word.id,
            state=2,
            stability=21.0,
            difficulty=5.0,
            due_at=FIXED_NOW - timedelta(hours=24),
            reps=2,
            lapses=0,
            last_review=FIXED_NOW - timedelta(days=2),
        ),
    )

    session = TrainingSession(
        user_id=USER_SCORING_ID,
        mode="mix",
        total_exercises=50,
        status="completed",
        created_at=FIXED_NOW - timedelta(days=40),
    )
    db_session.add(session)
    await db_session.flush()

    # 5 very old attempts (> 30 days ago) with score=0 and rating=1:
    # - excluded from grammar_score (because 40 newer attempts exist)
    # - excluded from 30d recall_part
    # - excluded from 14d consistency & weak_topics
    for idx in range(5):
        db_session.add(
            Attempt(
                session_id=session.id,
                user_id=USER_SCORING_ID,
                word_ids=[word.id],
                exercise_type="translation",
                level="B1",
                grammar_topic="Very Old Topic",
                question_payload={"task_text": "old"},
                user_answer="old answer",
                is_correct=False,
                grammar_score=0,
                fsrs_ratings=[{"word_id": word.id, "fsrs_rating": 1}],
                youtube_topic="Old YT",
                created_at=FIXED_NOW - timedelta(days=35, hours=idx),
            ),
        )

    # 20 attempts 20 days ago (inside 30d recall & last 40 grammar, OUTSIDE 14d window):
    # score = 100, rating = 4
    for idx in range(20):
        db_session.add(
            Attempt(
                session_id=session.id,
                user_id=USER_SCORING_ID,
                word_ids=[word.id],
                exercise_type="translation",
                level="B1",
                grammar_topic=f"Topic {idx % 6}",
                question_payload={"task_text": "mid"},
                user_answer="mid answer",
                is_correct=True,
                grammar_score=100,
                fsrs_ratings=[{"word_id": word.id, "fsrs_rating": 4}],
                youtube_topic="Mid YT",
                created_at=FIXED_NOW - timedelta(days=20, minutes=idx),
            ),
        )

    # 20 attempts 2 days ago (inside 14d window): score = 100, rating = 3
    for idx in range(20):
        db_session.add(
            Attempt(
                session_id=session.id,
                user_id=USER_SCORING_ID,
                word_ids=[word.id],
                exercise_type="translation",
                level="B1",
                grammar_topic=f"Topic {idx % 6}",
                question_payload={"task_text": "recent"},
                user_answer="recent answer",
                is_correct=True,
                grammar_score=100,
                fsrs_ratings=[{"word_id": word.id, "fsrs_rating": 3}],
                youtube_topic="Recent YT",
                created_at=FIXED_NOW - timedelta(days=2, minutes=idx),
            ),
        )

    # 1 unfinished attempt (user_answer IS NULL) with score=0 today -> MUST BE IGNORED
    db_session.add(
        Attempt(
            session_id=session.id,
            user_id=USER_SCORING_ID,
            word_ids=[word.id],
            exercise_type="fill_blank",
            level="A1",
            grammar_topic="Unfinished Topic",
            question_payload={"task_text": "___"},
            user_answer=None,
            is_correct=False,
            grammar_score=0,
            fsrs_ratings=[{"word_id": word.id, "fsrs_rating": 1}],
            youtube_topic="Unfinished YT",
            created_at=FIXED_NOW - timedelta(hours=1),
        ),
    )
    await db_session.flush()

    radar = await compute_user_radar(db_session, USER_SCORING_ID, now=FIXED_NOW)
    scores = radar.scores_by_axis()

    # grammar: all 40 newest completed attempts are B1, score 100, 6 unique topics -> 100
    assert scores["grammar"] == 100

    # vocab: 1 mastered word -> volume_part = (1/1500)**0.5; 40/40 Good/Easy in 30d -> recall = 1.0
    expected_vocab = round(100 * (0.75 * ((1.0 / 1500.0) ** 0.5) + 0.25 * 1.0))
    assert scores["vocab"] == expected_vocab

    # consistency: only 1 active day in last 14d (day -2; unfinished attempt on day 0 ignored),
    # 0 overdue > 24h out of 1 active word -> backlog_health = 1.0
    expected_consistency = round(100 * (0.6 * (1.0 / 12.0) + 0.4 * 1.0))
    assert scores["consistency"] == expected_consistency

    assert scores["writing"] == 0
    assert scores["listening"] == 0
    assert radar.weak_topics == []
