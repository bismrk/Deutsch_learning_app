"""Unit and PostgreSQL integration tests for the FSRS engine and target-word selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fsrs import Card, Rating, Scheduler, State
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.fsrs_engine import (
    CARD_STATE_LEARNING,
    CARD_STATE_NEW,
    CARD_STATE_RELEARNING,
    CARD_STATE_REVIEW,
    WordRatingInput,
    apply_fsrs_card_to_orm,
    apply_hint_anticheat,
    apply_word_ratings,
    create_default_scheduler,
    fsrs_card_to_orm,
    is_new_card,
    orm_to_fsrs_card,
    review_card,
    select_target_words,
)
from app.db.models import Deck, User, UserWordFSRS, Word

FIXED_NOW = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
FIXED_EARLIER = datetime(2026, 3, 10, 9, 0, 0, tzinfo=UTC)
KYIV_WINTER_TZ = timezone(timedelta(hours=2))

USER_A_ID = 5_111_222_333  # > 2^31 - 1 (64-bit BIGINT)
USER_B_ID = 6_444_555_666


def _make_orm_card(
    *,
    user_id: int = USER_A_ID,
    word_id: int = 101,
    state: int = CARD_STATE_NEW,
    stability: float = 0.0,
    difficulty: float = 0.0,
    due_at: datetime = FIXED_NOW,
    reps: int = 0,
    lapses: int = 0,
    last_review: datetime | None = None,
) -> UserWordFSRS:
    return UserWordFSRS(
        user_id=user_id,
        word_id=word_id,
        state=state,
        stability=stability,
        difficulty=difficulty,
        due_at=due_at,
        reps=reps,
        lapses=lapses,
        last_review=last_review,
    )


async def _seed_user_deck(
    db_session: AsyncSession,
    telegram_id: int,
    *,
    title: str = "Мій словник",
) -> tuple[User, Deck]:
    user = User(telegram_id=telegram_id)
    db_session.add(user)
    await db_session.flush()

    deck = Deck(owner_id=user.telegram_id, title=title, level="mixed")
    db_session.add(deck)
    await db_session.flush()
    return user, deck


async def _seed_word_card(
    db_session: AsyncSession,
    *,
    user_id: int,
    deck_id: int,
    word_de: str,
    translation_ua: str,
    part_of_speech: str | None = "noun",
    state: int = CARD_STATE_NEW,
    stability: float = 0.0,
    difficulty: float = 0.0,
    due_at: datetime = FIXED_NOW,
    reps: int = 0,
    lapses: int = 0,
    last_review: datetime | None = None,
) -> tuple[Word, UserWordFSRS]:
    word = Word(
        deck_id=deck_id,
        word_de=word_de,
        translation_ua=translation_ua,
        part_of_speech=part_of_speech,
        source="text",
    )
    db_session.add(word)
    await db_session.flush()

    card = UserWordFSRS(
        user_id=user_id,
        word_id=word.id,
        state=state,
        stability=stability,
        difficulty=difficulty,
        due_at=due_at,
        reps=reps,
        lapses=lapses,
        last_review=last_review,
    )
    db_session.add(card)
    await db_session.flush()
    return word, card


# ---------------------------------------------------------------------------
# Offline unit tests: bidirectional mapping, hint anti-cheat, FSRS transitions
# ---------------------------------------------------------------------------


def test_mapping_brand_new_card_round_trips_losslessly() -> None:
    row = _make_orm_card(word_id=42, due_at=FIXED_NOW)

    assert is_new_card(row) is True
    fsrs_card = orm_to_fsrs_card(row)

    assert fsrs_card.card_id == 42
    assert fsrs_card.state == State.Learning
    assert fsrs_card.step == 0
    assert fsrs_card.stability is None
    assert fsrs_card.difficulty is None
    assert fsrs_card.due == FIXED_NOW
    assert fsrs_card.due.tzinfo == UTC
    assert fsrs_card.last_review is None

    rebuilt = fsrs_card_to_orm(fsrs_card, user_id=USER_A_ID, word_id=42)
    assert rebuilt.user_id == USER_A_ID
    assert rebuilt.word_id == 42
    assert rebuilt.state == CARD_STATE_NEW
    assert rebuilt.stability == 0.0
    assert rebuilt.difficulty == 0.0
    assert rebuilt.due_at == FIXED_NOW
    assert rebuilt.due_at.tzinfo == UTC
    assert rebuilt.reps == 0
    assert rebuilt.lapses == 0
    assert rebuilt.last_review is None


def test_mapping_handles_unflushed_orm_defaults_and_partial_new_indicators() -> None:
    unflushed = UserWordFSRS(user_id=USER_A_ID, word_id=7, due_at=FIXED_NOW)
    assert is_new_card(unflushed) is True

    fsrs_card = orm_to_fsrs_card(unflushed)
    assert fsrs_card.state == State.Learning
    assert fsrs_card.step == 0
    assert fsrs_card.stability is None
    assert fsrs_card.difficulty is None
    assert fsrs_card.last_review is None

    # Card with non-zero state but reps == 0 or last_review is None is still treated as unreviewed
    zero_reps_row = _make_orm_card(state=CARD_STATE_REVIEW, reps=0, last_review=FIXED_EARLIER)
    assert is_new_card(zero_reps_row) is True
    assert orm_to_fsrs_card(zero_reps_row).last_review is None

    missing_last_review_row = _make_orm_card(state=CARD_STATE_LEARNING, reps=2, last_review=None)
    assert is_new_card(missing_last_review_row) is True
    assert orm_to_fsrs_card(missing_last_review_row).stability is None


def test_mapping_reviewed_cards_and_preserving_reps_lapses() -> None:
    review_row = _make_orm_card(
        word_id=55,
        state=CARD_STATE_REVIEW,
        stability=14.25,
        difficulty=4.75,
        due_at=FIXED_NOW + timedelta(days=5),
        reps=6,
        lapses=2,
        last_review=FIXED_EARLIER,
    )
    assert is_new_card(review_row) is False

    fsrs_card = orm_to_fsrs_card(review_row)
    assert fsrs_card.card_id == 55
    assert fsrs_card.state == State.Review
    assert fsrs_card.step is None
    assert fsrs_card.stability == pytest.approx(14.25)
    assert fsrs_card.difficulty == pytest.approx(4.75)
    assert fsrs_card.due == FIXED_NOW + timedelta(days=5)
    assert fsrs_card.last_review == FIXED_EARLIER

    # Applying back without overriding reps/lapses preserves existing counters
    target_row = _make_orm_card(
        word_id=55,
        state=CARD_STATE_NEW,
        reps=6,
        lapses=2,
    )
    apply_fsrs_card_to_orm(target_row, fsrs_card)
    assert target_row.state == CARD_STATE_REVIEW
    assert target_row.stability == pytest.approx(14.25)
    assert target_row.difficulty == pytest.approx(4.75)
    assert target_row.due_at == FIXED_NOW + timedelta(days=5)
    assert target_row.last_review == FIXED_EARLIER
    assert target_row.reps == 6
    assert target_row.lapses == 2

    # Learning (step 0 and step 1) and Relearning bidirectional mapping
    learning_step0_row = _make_orm_card(
        word_id=56,
        state=CARD_STATE_LEARNING,
        stability=0.212,
        difficulty=6.4,
        due_at=FIXED_EARLIER + timedelta(minutes=1),
        reps=1,
        lapses=0,
        last_review=FIXED_EARLIER,
    )
    learning_step0_card = orm_to_fsrs_card(learning_step0_row)
    assert learning_step0_card.state == State.Learning
    assert learning_step0_card.step == 0

    learning_step1_row = _make_orm_card(
        word_id=57,
        state=CARD_STATE_LEARNING,
        stability=2.3,
        difficulty=2.1,
        due_at=FIXED_EARLIER + timedelta(minutes=10),
        reps=1,
        lapses=0,
        last_review=FIXED_EARLIER,
    )
    learning_step1_card = orm_to_fsrs_card(learning_step1_row)
    assert learning_step1_card.state == State.Learning
    assert learning_step1_card.step == 1

    relearning_row = _make_orm_card(
        word_id=58,
        state=CARD_STATE_RELEARNING,
        stability=1.8,
        difficulty=7.9,
        due_at=FIXED_EARLIER + timedelta(minutes=10),
        reps=5,
        lapses=2,
        last_review=FIXED_EARLIER,
    )
    relearning_card = orm_to_fsrs_card(relearning_row)
    assert relearning_card.state == State.Relearning
    assert relearning_card.step == 0
    rebuilt_relearning = fsrs_card_to_orm(
        relearning_card,
        user_id=USER_A_ID,
        word_id=58,
        reps=5,
        lapses=2,
    )
    assert rebuilt_relearning.state == CARD_STATE_RELEARNING
    assert rebuilt_relearning.stability == pytest.approx(1.8)
    assert rebuilt_relearning.difficulty == pytest.approx(7.9)
    assert rebuilt_relearning.reps == 5
    assert rebuilt_relearning.lapses == 2


def test_mapping_normalizes_non_utc_aware_datetimes_and_rejects_naive_or_invalid() -> None:
    aware_kyiv_due = datetime(2026, 3, 15, 14, 0, 0, tzinfo=KYIV_WINTER_TZ)
    aware_kyiv_last = datetime(2026, 3, 10, 11, 0, 0, tzinfo=KYIV_WINTER_TZ)

    row = _make_orm_card(
        state=CARD_STATE_REVIEW,
        stability=8.0,
        difficulty=5.0,
        due_at=aware_kyiv_due,
        reps=3,
        lapses=0,
        last_review=aware_kyiv_last,
    )
    card = orm_to_fsrs_card(row)
    assert card.due == FIXED_NOW
    assert card.due.tzinfo == UTC
    assert card.last_review == FIXED_EARLIER
    assert card.last_review.tzinfo == UTC

    # Naive due_at / last_review / now must be rejected
    naive_dt = datetime(2026, 3, 15, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        orm_to_fsrs_card(_make_orm_card(due_at=naive_dt))
    with pytest.raises(ValueError, match="timezone-aware"):
        review_card(_make_orm_card(), rating=3, now=naive_dt)

    # Invalid ORM state / counters / non-positive stability on Review card
    with pytest.raises(ValueError, match="Unsupported FSRS card state"):
        orm_to_fsrs_card(_make_orm_card(state=4))
    with pytest.raises(ValueError, match="non-negative"):
        orm_to_fsrs_card(_make_orm_card(reps=-1))
    with pytest.raises(ValueError, match="positive stability and difficulty"):
        orm_to_fsrs_card(
            _make_orm_card(
                state=CARD_STATE_REVIEW,
                stability=0.0,
                difficulty=5.0,
                reps=1,
                last_review=FIXED_EARLIER,
            ),
        )


@pytest.mark.parametrize(
    ("raw_rating", "used_hint", "expected"),
    [
        (1, False, Rating.Again),
        (2, False, Rating.Hard),
        (3, False, Rating.Good),
        (4, False, Rating.Easy),
        (1, True, Rating.Again),  # Must stay 1, never raised to 2
        (2, True, Rating.Hard),
        (3, True, Rating.Hard),
        (4, True, Rating.Hard),
    ],
)
def test_apply_hint_anticheat_for_all_ratings(
    raw_rating: int,
    used_hint: bool,
    expected: Rating,
) -> None:
    assert apply_hint_anticheat(raw_rating, used_hint=used_hint) == expected


@pytest.mark.parametrize("invalid_rating", [0, 5, -1, 99, True, False, "3"])
@pytest.mark.parametrize("used_hint", [False, True])
def test_invalid_ratings_raise_value_error_without_mutating_card(
    invalid_rating: object,
    used_hint: bool,
) -> None:
    card = _make_orm_card(
        state=CARD_STATE_REVIEW,
        stability=10.0,
        difficulty=5.0,
        due_at=FIXED_NOW,
        reps=3,
        lapses=1,
        last_review=FIXED_EARLIER,
    )

    with pytest.raises(ValueError):
        apply_hint_anticheat(invalid_rating, used_hint=used_hint)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        review_card(card, invalid_rating, used_hint=used_hint, now=FIXED_NOW)  # type: ignore[arg-type]

    # Card must remain untouched after rejected rating
    assert card.state == CARD_STATE_REVIEW
    assert card.stability == 10.0
    assert card.difficulty == 5.0
    assert card.due_at == FIXED_NOW
    assert card.reps == 3
    assert card.lapses == 1
    assert card.last_review == FIXED_EARLIER


def test_review_card_all_four_ratings_monotonic_intervals_for_new_card() -> None:
    results: dict[int, UserWordFSRS] = {}
    for rating in (1, 2, 3, 4):
        row = _make_orm_card(word_id=10, due_at=FIXED_NOW)
        review_card(row, rating=rating, now=FIXED_NOW)
        results[rating] = row

        assert row.reps == 1
        assert row.lapses == 0  # Brand-new card never counts as a lapse on first review
        assert row.last_review == FIXED_NOW
        assert row.due_at.tzinfo == UTC
        assert row.due_at > FIXED_NOW
        assert row.stability > 0.0
        assert row.difficulty > 0.0

    assert results[1].state == CARD_STATE_LEARNING
    assert results[2].state == CARD_STATE_LEARNING
    assert results[3].state == CARD_STATE_LEARNING
    assert results[4].state == CARD_STATE_REVIEW

    assert (
        results[1].due_at
        <= results[2].due_at
        <= results[3].due_at
        <= results[4].due_at
    )
    assert results[1].due_at < results[4].due_at


def test_review_card_monotonic_intervals_and_lapse_for_review_card() -> None:
    last_review = FIXED_NOW - timedelta(days=12)
    initial_due = FIXED_NOW - timedelta(days=2)
    results: dict[int, UserWordFSRS] = {}

    for rating in (1, 2, 3, 4):
        row = _make_orm_card(
            word_id=20,
            state=CARD_STATE_REVIEW,
            stability=10.0,
            difficulty=5.0,
            due_at=initial_due,
            reps=4,
            lapses=1,
            last_review=last_review,
        )
        review_card(row, rating=rating, now=FIXED_NOW)
        results[rating] = row

        assert row.reps == 5
        assert row.last_review == FIXED_NOW
        assert row.due_at.tzinfo == UTC
        assert row.due_at > FIXED_NOW

    # Again on Review card transitions to Relearning and increments lapses
    assert results[1].state == CARD_STATE_RELEARNING
    assert results[1].lapses == 2
    assert results[1].stability < 10.0

    # Hard / Good / Easy stay in Review and keep lapses unchanged
    for rating in (2, 3, 4):
        assert results[rating].state == CARD_STATE_REVIEW
        assert results[rating].lapses == 1

    assert (
        results[1].due_at
        <= results[2].due_at
        <= results[3].due_at
        <= results[4].due_at
    )
    assert results[1].due_at < results[2].due_at < results[3].due_at < results[4].due_at


@pytest.mark.parametrize(
    ("initial_state", "interval_delta"),
    [
        (CARD_STATE_LEARNING, timedelta(minutes=1)),
        (CARD_STATE_LEARNING, timedelta(minutes=10)),
        (CARD_STATE_RELEARNING, timedelta(minutes=10)),
    ],
)
def test_review_card_monotonic_intervals_for_learning_and_relearning_cards(
    initial_state: int,
    interval_delta: timedelta,
) -> None:
    last_review = FIXED_NOW - interval_delta
    due_dates: list[datetime] = []

    for rating in (1, 2, 3, 4):
        row = _make_orm_card(
            word_id=25,
            state=initial_state,
            stability=2.0,
            difficulty=6.0,
            due_at=FIXED_NOW,
            reps=2,
            lapses=1 if initial_state == CARD_STATE_RELEARNING else 0,
            last_review=last_review,
        )
        review_card(row, rating=rating, now=FIXED_NOW)
        due_dates.append(row.due_at)
        assert row.due_at.tzinfo == UTC
        assert row.last_review == FIXED_NOW

    assert due_dates[0] <= due_dates[1] <= due_dates[2] <= due_dates[3]


def test_review_card_full_lifecycle_learning_graduation_lapse_and_relearning() -> None:
    row = _make_orm_card(word_id=30, due_at=FIXED_NOW)

    # 1. New -> Learning (step 0 -> step 1) with Good
    t1 = FIXED_NOW
    review_card(row, rating=3, now=t1)
    assert row.state == CARD_STATE_LEARNING
    assert row.reps == 1
    assert row.lapses == 0
    assert row.due_at == t1 + timedelta(minutes=10)

    # 2. Learning (step 1) -> Review on second Good
    t2 = row.due_at
    review_card(row, rating=3, now=t2)
    assert row.state == CARD_STATE_REVIEW
    assert row.reps == 2
    assert row.lapses == 0
    assert row.due_at >= t2 + timedelta(days=1)

    # 3. Review -> Relearning on Again (lapses increments)
    t3 = row.due_at
    review_card(row, rating=1, now=t3)
    assert row.state == CARD_STATE_RELEARNING
    assert row.reps == 3
    assert row.lapses == 1
    assert row.due_at == t3 + timedelta(minutes=10)

    # 4. Relearning -> Review on Good (lapses preserved)
    t4 = row.due_at
    review_card(row, rating=3, now=t4)
    assert row.state == CARD_STATE_REVIEW
    assert row.reps == 4
    assert row.lapses == 1
    assert row.due_at >= t4 + timedelta(days=1)


def test_review_card_again_on_already_reviewed_learning_card_increments_lapses() -> None:
    row = _make_orm_card(word_id=31, due_at=FIXED_NOW)

    review_card(row, rating=3, now=FIXED_NOW)
    assert row.state == CARD_STATE_LEARNING
    assert row.reps == 1
    assert row.lapses == 0

    review_card(row, rating=1, now=FIXED_NOW + timedelta(minutes=10))
    assert row.state == CARD_STATE_LEARNING
    assert row.reps == 2
    assert row.lapses == 1


def test_review_card_hint_anticheat_matches_capped_rating_exactly() -> None:
    last_review = FIXED_NOW - timedelta(days=10)
    due = FIXED_NOW - timedelta(days=1)

    for raw_rating, expected_effective in ((4, 2), (3, 2), (2, 2), (1, 1)):
        hinted_card = _make_orm_card(
            word_id=40,
            state=CARD_STATE_REVIEW,
            stability=9.0,
            difficulty=5.0,
            due_at=due,
            reps=3,
            lapses=0,
            last_review=last_review,
        )
        reference_card = _make_orm_card(
            word_id=40,
            state=CARD_STATE_REVIEW,
            stability=9.0,
            difficulty=5.0,
            due_at=due,
            reps=3,
            lapses=0,
            last_review=last_review,
        )

        review_card(hinted_card, rating=raw_rating, used_hint=True, now=FIXED_NOW)
        review_card(reference_card, rating=expected_effective, used_hint=False, now=FIXED_NOW)

        assert hinted_card.state == reference_card.state
        assert hinted_card.stability == pytest.approx(reference_card.stability)
        assert hinted_card.difficulty == pytest.approx(reference_card.difficulty)
        assert hinted_card.due_at == reference_card.due_at
        assert hinted_card.reps == reference_card.reps
        assert hinted_card.lapses == reference_card.lapses
        assert hinted_card.last_review == reference_card.last_review

    # Confirm rating=4 without hint strictly outperforms rating=4 with hint
    unhinted_easy = _make_orm_card(
        word_id=40,
        state=CARD_STATE_REVIEW,
        stability=9.0,
        difficulty=5.0,
        due_at=due,
        reps=3,
        lapses=0,
        last_review=last_review,
    )
    hinted_easy = _make_orm_card(
        word_id=40,
        state=CARD_STATE_REVIEW,
        stability=9.0,
        difficulty=5.0,
        due_at=due,
        reps=3,
        lapses=0,
        last_review=last_review,
    )
    review_card(unhinted_easy, rating=4, used_hint=False, now=FIXED_NOW)
    review_card(hinted_easy, rating=4, used_hint=True, now=FIXED_NOW)
    assert unhinted_easy.due_at > hinted_easy.due_at
    assert unhinted_easy.stability > hinted_easy.stability


def test_default_scheduler_matches_official_fsrs_directly() -> None:
    scheduler = create_default_scheduler()
    assert isinstance(scheduler, Scheduler)
    assert scheduler.enable_fuzzing is False

    row = _make_orm_card(word_id=77, due_at=FIXED_NOW)
    official_card = Card(
        card_id=77,
        state=State.Learning,
        step=0,
        stability=None,
        difficulty=None,
        due=FIXED_NOW,
        last_review=None,
    )
    expected_card, _ = scheduler.review_card(official_card, Rating.Easy, review_datetime=FIXED_NOW)

    review_card(row, rating=4, now=FIXED_NOW, scheduler=scheduler)
    assert row.state == int(expected_card.state)
    assert row.stability == pytest.approx(expected_card.stability)
    assert row.difficulty == pytest.approx(expected_card.difficulty)
    assert row.due_at == expected_card.due
    assert row.last_review == expected_card.last_review


# ---------------------------------------------------------------------------
# PostgreSQL integration tests (isolated TEST_DATABASE_URL + rollback)
# ---------------------------------------------------------------------------


async def test_select_target_words_empty_vocabulary_returns_empty_list(
    db_session: AsyncSession,
) -> None:
    await _seed_user_deck(db_session, USER_A_ID)
    selected = await select_target_words(db_session, USER_A_ID, count=3, now=FIXED_NOW)
    assert selected == []


async def test_select_target_words_rejects_invalid_count_and_naive_now(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        await select_target_words(db_session, USER_A_ID, count=0, now=FIXED_NOW)
    with pytest.raises(ValueError, match="positive integer"):
        await select_target_words(db_session, USER_A_ID, count=-2, now=FIXED_NOW)
    with pytest.raises(ValueError, match="integer"):
        await select_target_words(db_session, USER_A_ID, count=True, now=FIXED_NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        await select_target_words(
            db_session,
            USER_A_ID,
            count=2,
            now=datetime(2026, 3, 15, 12, 0, 0),
        )


async def test_select_target_words_tier1_overdue_priority_order_and_boundary(
    db_session: AsyncSession,
) -> None:
    _, deck = await _seed_user_deck(db_session, USER_A_ID)

    # All reviewed (reps > 0) and due_at <= FIXED_NOW (including exact equality boundary)
    w_exact_now, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="der Bahnhof",
        translation_ua="вокзал",
        state=CARD_STATE_REVIEW,
        stability=5.0,
        difficulty=7.0,
        due_at=FIXED_NOW,  # Exact boundary: due_at == now -> Tier 1
        reps=3,
        lapses=1,
        last_review=FIXED_EARLIER,
    )
    w_more_overdue_same_diff, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="die Verspätung",
        translation_ua="затримка",
        state=CARD_STATE_REVIEW,
        stability=5.0,
        difficulty=7.0,
        due_at=FIXED_NOW - timedelta(hours=6),
        reps=3,
        lapses=1,
        last_review=FIXED_EARLIER,
    )
    w_higher_difficulty, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="das Treffen",
        translation_ua="зустріч",
        state=CARD_STATE_REVIEW,
        stability=4.0,
        difficulty=8.5,
        due_at=FIXED_NOW - timedelta(minutes=5),
        reps=2,
        lapses=1,
        last_review=FIXED_EARLIER,
    )
    w_highest_lapses, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="der Zug",
        translation_ua="потяг",
        state=CARD_STATE_RELEARNING,
        stability=2.0,
        difficulty=4.0,
        due_at=FIXED_NOW - timedelta(minutes=1),
        reps=5,
        lapses=3,
        last_review=FIXED_EARLIER,
    )

    selected_top3 = await select_target_words(db_session, USER_A_ID, count=3, now=FIXED_NOW)
    assert [w.word_id for w in selected_top3] == [
        w_highest_lapses.id,  # lapses=3
        w_higher_difficulty.id,  # lapses=1, difficulty=8.5
        w_more_overdue_same_diff.id,  # lapses=1, difficulty=7.0, older due_at
    ]
    assert all(w.priority_tier == "overdue" for w in selected_top3)

    # Excluding the top 3 reveals the exact due_at == now card still in Tier 1
    remaining = await select_target_words(
        db_session,
        USER_A_ID,
        count=3,
        exclude_word_ids={w.word_id for w in selected_top3},
        now=FIXED_NOW,
    )
    assert [w.word_id for w in remaining] == [w_exact_now.id]
    assert remaining[0].priority_tier == "overdue"


async def test_select_target_words_tier2_new_words_only(
    db_session: AsyncSession,
) -> None:
    _, deck = await _seed_user_deck(db_session, USER_A_ID)

    w_new_2, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="der Stuhl",
        translation_ua="стілець",
        due_at=FIXED_NOW - timedelta(hours=1),
        reps=0,
    )
    w_new_1, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="der Tisch",
        translation_ua="стіл",
        due_at=FIXED_NOW - timedelta(hours=3),
        reps=0,
    )
    w_new_3, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="das Buch",
        translation_ua="книга",
        due_at=FIXED_NOW + timedelta(hours=1),  # Even future due_at with reps==0 is Tier 2
        reps=0,
    )

    selected = await select_target_words(db_session, USER_A_ID, count=3, now=FIXED_NOW)
    assert [w.word_id for w in selected] == [w_new_1.id, w_new_2.id, w_new_3.id]
    assert all(w.priority_tier == "new" for w in selected)


async def test_select_target_words_tier3_upcoming_nearest_due_first(
    db_session: AsyncSession,
) -> None:
    _, deck = await _seed_user_deck(db_session, USER_A_ID)

    # Farther word with high lapses/difficulty MUST NOT beat nearer due_at in Tier 3
    w_far_high_lapses, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="die Prüfung",
        translation_ua="іспит",
        state=CARD_STATE_REVIEW,
        stability=20.0,
        difficulty=9.5,
        due_at=FIXED_NOW + timedelta(days=10),
        reps=6,
        lapses=4,
        last_review=FIXED_EARLIER,
    )
    w_nearest, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="die Antwort",
        translation_ua="відповідь",
        state=CARD_STATE_REVIEW,
        stability=12.0,
        difficulty=3.0,
        due_at=FIXED_NOW + timedelta(minutes=30),
        reps=2,
        lapses=0,
        last_review=FIXED_EARLIER,
    )
    w_middle, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="die Frage",
        translation_ua="питання",
        state=CARD_STATE_REVIEW,
        stability=15.0,
        difficulty=5.0,
        due_at=FIXED_NOW + timedelta(days=2),
        reps=3,
        lapses=1,
        last_review=FIXED_EARLIER,
    )

    selected = await select_target_words(db_session, USER_A_ID, count=3, now=FIXED_NOW)
    assert [w.word_id for w in selected] == [
        w_nearest.id,
        w_middle.id,
        w_far_high_lapses.id,
    ]
    assert all(w.priority_tier == "upcoming" for w in selected)


async def test_select_target_words_combined_three_tiers_small_vocab_and_exclusions(
    db_session: AsyncSession,
) -> None:
    _, deck = await _seed_user_deck(db_session, USER_A_ID)

    w_upcoming_far, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="der Urlaub",
        translation_ua="відпустка",
        state=CARD_STATE_REVIEW,
        stability=25.0,
        difficulty=4.0,
        due_at=FIXED_NOW + timedelta(days=7),
        reps=4,
        lapses=0,
        last_review=FIXED_EARLIER,
    )
    w_upcoming_near, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="der Koffer",
        translation_ua="валіза",
        state=CARD_STATE_REVIEW,
        stability=8.0,
        difficulty=5.0,
        due_at=FIXED_NOW + timedelta(hours=4),
        reps=2,
        lapses=0,
        last_review=FIXED_EARLIER,
    )
    w_new, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="die Fahrkarte",
        translation_ua="квиток",
        due_at=FIXED_NOW - timedelta(days=5),  # reps==0 stays in Tier 2 behind overdue reviewed
        reps=0,
    )
    w_overdue, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="der Ausgang",
        translation_ua="вихід",
        state=CARD_STATE_REVIEW,
        stability=3.0,
        difficulty=6.5,
        due_at=FIXED_NOW - timedelta(hours=2),
        reps=2,
        lapses=1,
        last_review=FIXED_EARLIER,
    )

    # Combined 3-tier ordering for count=1, 2, 3 without duplicates
    one = await select_target_words(db_session, USER_A_ID, count=1, now=FIXED_NOW)
    assert [w.word_id for w in one] == [w_overdue.id]

    two = await select_target_words(db_session, USER_A_ID, count=2, now=FIXED_NOW)
    assert [w.word_id for w in two] == [w_overdue.id, w_new.id]

    three = await select_target_words(
        db_session,
        USER_A_ID,
        count=3,
        exclude_word_ids=set(),  # Empty set must work cleanly
        now=FIXED_NOW,
    )
    assert [w.word_id for w in three] == [w_overdue.id, w_new.id, w_upcoming_near.id]
    assert [w.priority_tier for w in three] == ["overdue", "new", "upcoming"]
    assert len({w.word_id for w in three}) == 3

    # Exclude overdue + new -> returns remaining 2 (< count=3) in nearest-due order
    after_exclude = await select_target_words(
        db_session,
        USER_A_ID,
        count=3,
        exclude_word_ids=[w_overdue.id, w_new.id, w_overdue.id],
        now=FIXED_NOW,
    )
    assert [w.word_id for w in after_exclude] == [w_upcoming_near.id, w_upcoming_far.id]

    # Exclude all words -> returns []
    all_excluded = await select_target_words(
        db_session,
        USER_A_ID,
        count=3,
        exclude_word_ids={w_overdue.id, w_new.id, w_upcoming_near.id, w_upcoming_far.id},
        now=FIXED_NOW,
    )
    assert all_excluded == []


async def test_select_target_words_small_vocabulary_fewer_than_count(
    db_session: AsyncSession,
) -> None:
    _, deck = await _seed_user_deck(db_session, USER_A_ID)
    w_only, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="das Haus",
        translation_ua="будинок",
        due_at=FIXED_NOW,
        reps=0,
    )

    selected = await select_target_words(db_session, USER_A_ID, count=3, now=FIXED_NOW)
    assert [w.word_id for w in selected] == [w_only.id]
    assert selected[0].word_de == "das Haus"
    assert selected[0].translation_ua == "будинок"


async def test_select_target_words_isolates_user_a_from_user_b(
    db_session: AsyncSession,
) -> None:
    _, deck_a = await _seed_user_deck(db_session, USER_A_ID)
    _, deck_b = await _seed_user_deck(db_session, USER_B_ID)

    # User B has high-priority overdue cards
    w_b1, _ = await _seed_word_card(
        db_session,
        user_id=USER_B_ID,
        deck_id=deck_b.id,
        word_de="der Fehler",
        translation_ua="помилка",
        state=CARD_STATE_RELEARNING,
        stability=1.0,
        difficulty=9.0,
        due_at=FIXED_NOW - timedelta(days=5),
        reps=10,
        lapses=5,
        last_review=FIXED_EARLIER,
    )
    w_b2, _ = await _seed_word_card(
        db_session,
        user_id=USER_B_ID,
        deck_id=deck_b.id,
        word_de="die Lösung",
        translation_ua="рішення",
        due_at=FIXED_NOW - timedelta(days=1),
        reps=0,
    )

    # User A has empty vocab -> must get []
    assert await select_target_words(db_session, USER_A_ID, count=3, now=FIXED_NOW) == []

    # Add 1 upcoming card for User A -> User A gets ONLY their own word
    w_a1, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck_a.id,
        word_de="der Anfang",
        translation_ua="початок",
        state=CARD_STATE_REVIEW,
        stability=15.0,
        difficulty=4.0,
        due_at=FIXED_NOW + timedelta(days=3),
        reps=2,
        lapses=0,
        last_review=FIXED_EARLIER,
    )

    selected_a = await select_target_words(db_session, USER_A_ID, count=3, now=FIXED_NOW)
    assert [w.word_id for w in selected_a] == [w_a1.id]

    selected_b = await select_target_words(db_session, USER_B_ID, count=3, now=FIXED_NOW)
    assert [w.word_id for w in selected_b] == [w_b1.id, w_b2.id]


async def test_apply_word_ratings_batch_hint_anticheat_and_selection_feedback_loop(
    db_session: AsyncSession,
) -> None:
    _, deck = await _seed_user_deck(db_session, USER_A_ID)

    w_new_easy, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="der Fluss",
        translation_ua="річка",
        due_at=FIXED_NOW - timedelta(hours=2),
        reps=0,
    )
    w_review_hinted_easy, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="die Brücke",
        translation_ua="міст",
        state=CARD_STATE_REVIEW,
        stability=10.0,
        difficulty=5.0,
        due_at=FIXED_NOW - timedelta(days=1),
        reps=3,
        lapses=0,
        last_review=FIXED_NOW - timedelta(days=11),
    )
    w_review_hinted_again, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="der Berg",
        translation_ua="гора",
        state=CARD_STATE_REVIEW,
        stability=10.0,
        difficulty=5.0,
        due_at=FIXED_NOW - timedelta(days=1),
        reps=3,
        lapses=0,
        last_review=FIXED_NOW - timedelta(days=11),
    )

    # Batch 1: without hint (dict input)
    res_unhinted = await apply_word_ratings(
        db_session,
        USER_A_ID,
        {w_new_easy.id: 4},
        used_hint=False,
        now=FIXED_NOW,
    )
    assert len(res_unhinted) == 1
    assert res_unhinted[0].word_id == w_new_easy.id
    assert res_unhinted[0].raw_rating == 4
    assert res_unhinted[0].effective_rating == 4
    assert res_unhinted[0].state == CARD_STATE_REVIEW
    assert res_unhinted[0].reps == 1
    assert res_unhinted[0].lapses == 0
    assert res_unhinted[0].due_at > FIXED_NOW + timedelta(days=1)

    # Batch 2: with hint (all 4 ratings: 1->1, 2->2, 3->2, 4->2)
    w_review_hinted_hard, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="der Wald",
        translation_ua="ліс",
        state=CARD_STATE_REVIEW,
        stability=10.0,
        difficulty=5.0,
        due_at=FIXED_NOW - timedelta(days=1),
        reps=3,
        lapses=0,
        last_review=FIXED_NOW - timedelta(days=11),
    )
    w_review_hinted_good, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="der See",
        translation_ua="озеро",
        state=CARD_STATE_REVIEW,
        stability=10.0,
        difficulty=5.0,
        due_at=FIXED_NOW - timedelta(days=1),
        reps=3,
        lapses=0,
        last_review=FIXED_NOW - timedelta(days=11),
    )

    res_hinted = await apply_word_ratings(
        db_session,
        USER_A_ID,
        [
            WordRatingInput(word_id=w_review_hinted_easy.id, rating=4),
            WordRatingInput(word_id=w_review_hinted_good.id, rating=3),
            (w_review_hinted_hard.id, 2),
            (w_review_hinted_again.id, 1),
        ],
        used_hint=True,
        now=FIXED_NOW,
    )
    assert [(r.word_id, r.raw_rating, r.effective_rating) for r in res_hinted] == [
        (w_review_hinted_easy.id, 4, 2),
        (w_review_hinted_good.id, 3, 2),
        (w_review_hinted_hard.id, 2, 2),
        (w_review_hinted_again.id, 1, 1),
    ]
    # 4, 3, and 2 with hint all produce the exact same Hard due_at and stability
    assert res_hinted[0].due_at == res_hinted[1].due_at == res_hinted[2].due_at
    assert res_hinted[0].stability == pytest.approx(res_hinted[2].stability)
    assert res_hinted[1].stability == pytest.approx(res_hinted[2].stability)

    hinted_easy_id = w_review_hinted_easy.id
    hinted_again_id = w_review_hinted_again.id

    await db_session.commit()
    db_session.expire_all()

    card_easy_hinted = await db_session.get(
        UserWordFSRS,
        (USER_A_ID, hinted_easy_id),
    )
    card_again_hinted = await db_session.get(
        UserWordFSRS,
        (USER_A_ID, hinted_again_id),
    )
    assert card_easy_hinted is not None
    assert card_again_hinted is not None

    assert card_easy_hinted.state == CARD_STATE_REVIEW
    assert card_easy_hinted.reps == 4
    assert card_easy_hinted.lapses == 0
    assert card_easy_hinted.last_review == FIXED_NOW

    assert card_again_hinted.state == CARD_STATE_RELEARNING
    assert card_again_hinted.reps == 4
    assert card_again_hinted.lapses == 1
    assert card_again_hinted.last_review == FIXED_NOW
    assert card_again_hinted.due_at == FIXED_NOW + timedelta(minutes=10)

    # 10 minutes later, the lapsed Relearning card is due first in select_target_words
    next_batch = await select_target_words(
        db_session,
        USER_A_ID,
        count=3,
        now=FIXED_NOW + timedelta(minutes=10),
    )
    assert next_batch[0].word_id == hinted_again_id
    assert next_batch[0].priority_tier == "overdue"


async def test_apply_word_ratings_strict_user_isolation_and_shared_deck(
    db_session: AsyncSession,
) -> None:
    _, deck_a = await _seed_user_deck(db_session, USER_A_ID)
    _, deck_b = await _seed_user_deck(db_session, USER_B_ID)

    # Shared deck (owner_id=None) where both User A and User B have their own FSRS cards
    shared_deck = Deck(owner_id=None, title="Goethe A1", level="A1")
    db_session.add(shared_deck)
    await db_session.flush()

    w_a_private, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck_a.id,
        word_de="die Stadt",
        translation_ua="місто",
        due_at=FIXED_EARLIER,
        reps=0,
    )
    w_b_private, card_b_private = await _seed_word_card(
        db_session,
        user_id=USER_B_ID,
        deck_id=deck_b.id,
        word_de="das Dorf",
        translation_ua="село",
        state=CARD_STATE_REVIEW,
        stability=12.0,
        difficulty=5.5,
        due_at=FIXED_EARLIER,
        reps=4,
        lapses=1,
        last_review=FIXED_EARLIER - timedelta(days=5),
    )

    shared_word = Word(
        deck_id=shared_deck.id,
        word_de="das Wasser",
        translation_ua="вода",
        part_of_speech="noun",
        source="system",
    )
    db_session.add(shared_word)
    await db_session.flush()

    card_a_shared = UserWordFSRS(
        user_id=USER_A_ID,
        word_id=shared_word.id,
        due_at=FIXED_EARLIER,
    )
    card_b_shared = UserWordFSRS(
        user_id=USER_B_ID,
        word_id=shared_word.id,
        state=CARD_STATE_REVIEW,
        stability=9.5,
        difficulty=4.5,
        due_at=FIXED_EARLIER,
        reps=2,
        lapses=0,
        last_review=FIXED_EARLIER - timedelta(days=3),
    )
    db_session.add_all([card_a_shared, card_b_shared])
    await db_session.commit()

    # Snapshot User B's cards before User A's batch update
    b_private_before = (
        card_b_private.state,
        card_b_private.stability,
        card_b_private.difficulty,
        card_b_private.due_at,
        card_b_private.reps,
        card_b_private.lapses,
        card_b_private.last_review,
    )
    b_shared_before = (
        card_b_shared.state,
        card_b_shared.stability,
        card_b_shared.difficulty,
        card_b_shared.due_at,
        card_b_shared.reps,
        card_b_shared.lapses,
        card_b_shared.last_review,
    )

    a_private_id = w_a_private.id
    b_private_id = w_b_private.id
    shared_word_id = shared_word.id

    # User A attempts to rate ONLY User B's private word and non-existent IDs -> empty result
    foreign_only_results = await apply_word_ratings(
        db_session,
        USER_A_ID,
        {b_private_id: 4, 999_999: 3},
        used_hint=False,
        now=FIXED_NOW,
    )
    assert foreign_only_results == []

    # User A attempts to rate own words, User B's private word, non-existent IDs, and huge ID
    results = await apply_word_ratings(
        db_session,
        USER_A_ID,
        {
            a_private_id: 3,
            shared_word_id: 4,
            b_private_id: 1,
            999_999: 2,
            9_999_999_999: 3,
        },
        used_hint=False,
        now=FIXED_NOW,
    )
    await db_session.commit()
    db_session.expire_all()

    # Only User A's 2 cards were updated
    assert [r.word_id for r in results] == [a_private_id, shared_word_id]

    refreshed_a_private = await db_session.get(UserWordFSRS, (USER_A_ID, a_private_id))
    refreshed_a_shared = await db_session.get(UserWordFSRS, (USER_A_ID, shared_word_id))
    assert refreshed_a_private is not None and refreshed_a_private.reps == 1
    assert refreshed_a_shared is not None and refreshed_a_shared.reps == 1

    # User A did NOT create a card for User B's private word
    assert await db_session.get(UserWordFSRS, (USER_A_ID, b_private_id)) is None

    # User B's private and shared cards remain 100% untouched
    refreshed_b_private = await db_session.get(UserWordFSRS, (USER_B_ID, b_private_id))
    refreshed_b_shared = await db_session.get(UserWordFSRS, (USER_B_ID, shared_word_id))
    assert refreshed_b_private is not None
    assert refreshed_b_shared is not None
    assert (
        refreshed_b_private.state,
        refreshed_b_private.stability,
        refreshed_b_private.difficulty,
        refreshed_b_private.due_at,
        refreshed_b_private.reps,
        refreshed_b_private.lapses,
        refreshed_b_private.last_review,
    ) == b_private_before
    assert (
        refreshed_b_shared.state,
        refreshed_b_shared.stability,
        refreshed_b_shared.difficulty,
        refreshed_b_shared.due_at,
        refreshed_b_shared.reps,
        refreshed_b_shared.lapses,
        refreshed_b_shared.last_review,
    ) == b_shared_before


async def test_apply_word_ratings_validates_all_before_mutating_and_handles_empty(
    db_session: AsyncSession,
) -> None:
    _, deck = await _seed_user_deck(db_session, USER_A_ID)
    w1, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="die Zeit",
        translation_ua="час",
        due_at=FIXED_EARLIER,
        reps=0,
    )
    w2, _ = await _seed_word_card(
        db_session,
        user_id=USER_A_ID,
        deck_id=deck.id,
        word_de="das Jahr",
        translation_ua="рік",
        due_at=FIXED_EARLIER,
        reps=0,
    )
    await db_session.commit()

    # Empty batch returns [] without error
    assert await apply_word_ratings(db_session, USER_A_ID, {}, now=FIXED_NOW) == []
    assert await apply_word_ratings(db_session, USER_A_ID, [], now=FIXED_NOW) == []

    # Invalid rating on 2nd entry must fail before mutating 1st entry
    with pytest.raises(ValueError, match="between 1 and 4"):
        await apply_word_ratings(
            db_session,
            USER_A_ID,
            [(w1.id, 3), (w2.id, 5)],
            now=FIXED_NOW,
        )

    cards = (
        await db_session.execute(
            select(UserWordFSRS)
            .where(UserWordFSRS.user_id == USER_A_ID)
            .order_by(UserWordFSRS.word_id.asc()),
        )
    ).scalars().all()
    assert [c.reps for c in cards] == [0, 0]
    assert [c.last_review for c in cards] == [None, None]
