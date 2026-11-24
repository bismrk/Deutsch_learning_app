"""FSRS spaced-repetition engine and SQL target-word prioritization."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fsrs import Card, Rating, Scheduler, State
from sqlalchemy import and_, case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserWordFSRS, Word

CARD_STATE_NEW = 0
CARD_STATE_LEARNING = int(State.Learning)
CARD_STATE_REVIEW = int(State.Review)
CARD_STATE_RELEARNING = int(State.Relearning)

_VALID_ORM_STATES = frozenset(
    {
        CARD_STATE_NEW,
        CARD_STATE_LEARNING,
        CARD_STATE_REVIEW,
        CARD_STATE_RELEARNING,
    },
)

MIN_RATING = int(Rating.Again)
MAX_RATING = int(Rating.Easy)
HINT_MAX_RATING = int(Rating.Hard)

_PG_INT32_MAX = 2_147_483_647

PriorityTier = Literal["overdue", "new", "upcoming"]

_TIER_CODE_TO_NAME: dict[int, PriorityTier] = {
    1: "overdue",
    2: "new",
    3: "upcoming",
}


def create_default_scheduler() -> Scheduler:
    """Create the default deterministic official FSRS scheduler (fuzzing disabled)."""
    return Scheduler(enable_fuzzing=False)


_DEFAULT_SCHEDULER = create_default_scheduler()


@dataclass(frozen=True, slots=True)
class SelectedWord:
    """Target vocabulary word selected for an exercise along with its FSRS card state."""

    word_id: int
    word_de: str
    translation_ua: str
    part_of_speech: str | None
    state: int
    stability: float
    difficulty: float
    due_at: datetime
    reps: int
    lapses: int
    last_review: datetime | None
    priority_tier: PriorityTier


@dataclass(frozen=True, slots=True)
class WordRatingInput:
    """Single word evaluation input for batch FSRS card updates."""

    word_id: int
    rating: int


@dataclass(frozen=True, slots=True)
class WordRatingResult:
    """Result of reviewing a single user word card with FSRS."""

    word_id: int
    raw_rating: int
    effective_rating: int
    state: int
    stability: float
    difficulty: float
    due_at: datetime
    reps: int
    lapses: int
    last_review: datetime


def _ensure_utc(value: datetime, *, field_name: str = "datetime") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _resolve_now_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return _ensure_utc(now, field_name="now")


def _validate_int_not_bool(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer, got {value!r}")
    return int(value)


def apply_hint_anticheat(rating: int | Rating, *, used_hint: bool = False) -> Rating:
    """Validate an FSRS rating in ``1..4`` and apply backend hint anti-cheat ``min(rating, 2)``."""
    raw_rating = _validate_int_not_bool(rating, field_name="rating")
    if raw_rating < MIN_RATING or raw_rating > MAX_RATING:
        raise ValueError(
            f"FSRS rating must be between {MIN_RATING} and {MAX_RATING}, got {raw_rating}",
        )
    effective = min(raw_rating, HINT_MAX_RATING) if used_hint else raw_rating
    return Rating(effective)


def is_new_card(card_row: UserWordFSRS) -> bool:
    """Return ``True`` when the ORM card row represents a brand-new unreviewed card."""
    state = CARD_STATE_NEW if card_row.state is None else card_row.state
    reps = 0 if card_row.reps is None else card_row.reps
    return state == CARD_STATE_NEW or reps == 0 or card_row.last_review is None


def _infer_step(
    state: State,
    *,
    due_at: datetime,
    last_review: datetime | None,
    scheduler: Scheduler,
) -> int | None:
    if state == State.Review:
        return None
    steps = scheduler.learning_steps if state == State.Learning else scheduler.relearning_steps
    if not steps or last_review is None:
        return 0
    scheduled_interval = due_at - last_review
    inferred_step = 0
    for index, step_duration in enumerate(steps):
        if scheduled_interval >= step_duration:
            inferred_step = index
    return inferred_step


def orm_to_fsrs_card(
    card_row: UserWordFSRS,
    *,
    now: datetime | None = None,
    scheduler: Scheduler | None = None,
) -> Card:
    """Convert a ``UserWordFSRS`` ORM row into an official ``fsrs.Card`` object.

    New unreviewed cards (``state == 0``, ``reps == 0``, or ``last_review is None``)
    are mapped to a fresh ``fsrs.Card`` with ``state=State.Learning``, ``step=0``,
    ``stability=None``, ``difficulty=None``, and ``last_review=None``.
    """
    state = CARD_STATE_NEW if card_row.state is None else card_row.state
    reps = 0 if card_row.reps is None else card_row.reps
    lapses = 0 if card_row.lapses is None else card_row.lapses
    stability_val = 0.0 if card_row.stability is None else float(card_row.stability)
    difficulty_val = 0.0 if card_row.difficulty is None else float(card_row.difficulty)

    if state not in _VALID_ORM_STATES:
        raise ValueError(f"Unsupported FSRS card state: {state!r}")
    if reps < 0:
        raise ValueError(f"Card reps must be non-negative, got {reps}")
    if lapses < 0:
        raise ValueError(f"Card lapses must be non-negative, got {lapses}")
    if stability_val < 0.0:
        raise ValueError(f"Card stability must be non-negative, got {stability_val}")
    if difficulty_val < 0.0:
        raise ValueError(f"Card difficulty must be non-negative, got {difficulty_val}")

    if card_row.due_at is not None:
        due_utc = _ensure_utc(card_row.due_at, field_name="due_at")
    else:
        due_utc = _resolve_now_utc(now)

    card_id = int(card_row.word_id) if card_row.word_id is not None else 0

    if is_new_card(card_row):
        return Card(
            card_id=card_id,
            state=State.Learning,
            step=0,
            stability=None,
            difficulty=None,
            due=due_utc,
            last_review=None,
        )

    assert card_row.last_review is not None
    last_review_utc = _ensure_utc(card_row.last_review, field_name="last_review")
    fsrs_state = State(state)
    active_scheduler = scheduler if scheduler is not None else _DEFAULT_SCHEDULER
    step = _infer_step(
        fsrs_state,
        due_at=due_utc,
        last_review=last_review_utc,
        scheduler=active_scheduler,
    )

    if fsrs_state in (State.Review, State.Relearning):
        if stability_val <= 0.0 or difficulty_val <= 0.0:
            raise ValueError(
                "Reviewed FSRS card in Review or Relearning state must have "
                "positive stability and difficulty",
            )
        card_stability: float | None = stability_val
        card_difficulty: float | None = difficulty_val
    else:
        card_stability = stability_val if stability_val > 0.0 else None
        card_difficulty = difficulty_val if difficulty_val > 0.0 else None

    return Card(
        card_id=card_id,
        state=fsrs_state,
        step=step,
        stability=card_stability,
        difficulty=card_difficulty,
        due=due_utc,
        last_review=last_review_utc,
    )


def apply_fsrs_card_to_orm(
    card_row: UserWordFSRS,
    fsrs_card: Card,
    *,
    reps: int | None = None,
    lapses: int | None = None,
) -> UserWordFSRS:
    """Write ``fsrs.Card`` scheduling fields back onto a ``UserWordFSRS`` ORM instance.

    Preserves ``reps`` and ``lapses`` unless explicitly supplied.
    """
    if reps is not None:
        validated_reps = _validate_int_not_bool(reps, field_name="reps")
        if validated_reps < 0:
            raise ValueError(f"reps must be non-negative, got {validated_reps}")
    else:
        validated_reps = None

    if lapses is not None:
        validated_lapses = _validate_int_not_bool(lapses, field_name="lapses")
        if validated_lapses < 0:
            raise ValueError(f"lapses must be non-negative, got {validated_lapses}")
    else:
        validated_lapses = None

    due_utc = _ensure_utc(fsrs_card.due, field_name="card.due")
    last_review_utc = (
        _ensure_utc(fsrs_card.last_review, field_name="card.last_review")
        if fsrs_card.last_review is not None
        else None
    )

    is_unreviewed_card = (
        last_review_utc is None
        and fsrs_card.stability is None
        and fsrs_card.difficulty is None
    )

    if is_unreviewed_card:
        card_row.state = CARD_STATE_NEW
        card_row.stability = 0.0
        card_row.difficulty = 0.0
        card_row.due_at = due_utc
        card_row.last_review = None
    else:
        card_row.state = int(fsrs_card.state)
        card_row.stability = float(fsrs_card.stability) if fsrs_card.stability is not None else 0.0
        card_row.difficulty = (
            float(fsrs_card.difficulty) if fsrs_card.difficulty is not None else 0.0
        )
        card_row.due_at = due_utc
        card_row.last_review = last_review_utc

    if validated_reps is not None:
        card_row.reps = validated_reps
    elif card_row.reps is None:
        card_row.reps = 0

    if validated_lapses is not None:
        card_row.lapses = validated_lapses
    elif card_row.lapses is None:
        card_row.lapses = 0

    return card_row


def fsrs_card_to_orm(
    fsrs_card: Card,
    *,
    user_id: int,
    word_id: int,
    reps: int = 0,
    lapses: int = 0,
) -> UserWordFSRS:
    """Create a new ``UserWordFSRS`` ORM instance populated from an ``fsrs.Card``."""
    row = UserWordFSRS(user_id=user_id, word_id=word_id)
    return apply_fsrs_card_to_orm(row, fsrs_card, reps=reps, lapses=lapses)


def review_card(
    card_row: UserWordFSRS,
    rating: int | Rating,
    *,
    used_hint: bool = False,
    now: datetime | None = None,
    scheduler: Scheduler | None = None,
) -> UserWordFSRS:
    """Review a ``UserWordFSRS`` card with FSRS using a rating in ``1..4`` and UTC ``now``.

    Applies backend hint anti-cheat (``min(rating, 2)`` when ``used_hint=True``), updates
    ``state``, ``stability``, ``difficulty``, ``due_at``, ``last_review``, increments
    ``reps`` by ``1``, and increments ``lapses`` by ``1`` when an already-reviewed card
    receives ``Again (1)``. Mutates ``card_row`` only after all inputs are validated.
    """
    effective_rating = apply_hint_anticheat(rating, used_hint=used_hint)
    now_utc = _resolve_now_utc(now)
    active_scheduler = scheduler if scheduler is not None else _DEFAULT_SCHEDULER

    was_new = is_new_card(card_row)
    current_reps = 0 if card_row.reps is None else card_row.reps
    current_lapses = 0 if card_row.lapses is None else card_row.lapses

    fsrs_card = orm_to_fsrs_card(card_row, now=now_utc, scheduler=active_scheduler)
    updated_card, _ = active_scheduler.review_card(
        fsrs_card,
        effective_rating,
        review_datetime=now_utc,
    )

    next_reps = current_reps + 1
    next_lapses = current_lapses + (1 if (effective_rating == Rating.Again and not was_new) else 0)

    return apply_fsrs_card_to_orm(
        card_row,
        updated_card,
        reps=next_reps,
        lapses=next_lapses,
    )


async def select_target_words(
    db: AsyncSession,
    user_id: int,
    count: int = 3,
    exclude_word_ids: Collection[int] | None = None,
    now: datetime | None = None,
) -> list[SelectedWord]:
    """Select target words for a user using 3-tier FSRS SQL prioritization.

    Priority tiers (strictly scoped to ``user_id``, deduplicated by ``word_id``):
    1. Overdue/due reviewed words (``reps > 0`` and ``due_at <= now``) ordered by
       ``lapses DESC``, ``difficulty DESC``, ``due_at ASC``.
    2. Brand-new words (``reps == 0``) ordered by ``due_at ASC``, ``word_id ASC``.
    3. Fallback upcoming words (``reps > 0`` and ``due_at > now``) ordered by
       nearest ``due_at ASC``.
    """
    validated_count = _validate_int_not_bool(count, field_name="count")
    if validated_count < 1:
        raise ValueError(f"count must be a positive integer, got {validated_count}")

    now_utc = _resolve_now_utc(now)

    tier_expr = case(
        (and_(UserWordFSRS.reps > 0, UserWordFSRS.due_at <= now_utc), 1),
        (UserWordFSRS.reps == 0, 2),
        else_=3,
    )
    tier1_lapses = case((tier_expr == 1, UserWordFSRS.lapses), else_=0)
    tier1_difficulty = case((tier_expr == 1, UserWordFSRS.difficulty), else_=0.0)

    filters = [UserWordFSRS.user_id == user_id]
    if exclude_word_ids:
        excluded_valid_ids = sorted(
            {
                wid
                for wid in (
                    _validate_int_not_bool(item, field_name="exclude_word_ids item")
                    for item in exclude_word_ids
                )
                if 1 <= wid <= _PG_INT32_MAX
            },
        )
        if excluded_valid_ids:
            filters.append(UserWordFSRS.word_id.notin_(excluded_valid_ids))

    stmt = (
        select(UserWordFSRS, Word, tier_expr.label("priority_tier_code"))
        .join(Word, Word.id == UserWordFSRS.word_id)
        .where(*filters)
        .order_by(
            tier_expr.asc(),
            tier1_lapses.desc(),
            tier1_difficulty.desc(),
            UserWordFSRS.due_at.asc(),
            UserWordFSRS.lapses.desc(),
            UserWordFSRS.difficulty.desc(),
            UserWordFSRS.word_id.asc(),
        )
        .limit(validated_count)
    )

    rows = (await db.execute(stmt)).all()
    return [
        SelectedWord(
            word_id=word.id,
            word_de=word.word_de,
            translation_ua=word.translation_ua,
            part_of_speech=word.part_of_speech,
            state=card.state,
            stability=card.stability,
            difficulty=card.difficulty,
            due_at=_ensure_utc(card.due_at, field_name="due_at"),
            reps=card.reps,
            lapses=card.lapses,
            last_review=(
                _ensure_utc(card.last_review, field_name="last_review")
                if card.last_review is not None
                else None
            ),
            priority_tier=_TIER_CODE_TO_NAME[int(tier_code)],
        )
        for card, word, tier_code in rows
    ]


def _normalize_ratings_batch(
    ratings: Mapping[int, int] | Sequence[tuple[int, int] | WordRatingInput],
    *,
    used_hint: bool,
) -> list[tuple[int, int, Rating]]:
    raw_items: list[tuple[int, int]] = []
    if isinstance(ratings, Mapping):
        for raw_word_id, raw_rating in ratings.items():
            word_id = _validate_int_not_bool(raw_word_id, field_name="word_id")
            rating = _validate_int_not_bool(raw_rating, field_name="rating")
            raw_items.append((word_id, rating))
    else:
        for item in ratings:
            if isinstance(item, WordRatingInput):
                word_id = _validate_int_not_bool(item.word_id, field_name="word_id")
                rating = _validate_int_not_bool(item.rating, field_name="rating")
            elif isinstance(item, tuple) and len(item) == 2:
                word_id_obj, rating_obj = item
                word_id = _validate_int_not_bool(word_id_obj, field_name="word_id")
                rating = _validate_int_not_bool(rating_obj, field_name="rating")
            else:
                raise ValueError(
                    "Each rating entry must be a WordRatingInput or a (word_id, rating) tuple",
                )
            raw_items.append((word_id, rating))

    return [
        (word_id, raw_rating, apply_hint_anticheat(raw_rating, used_hint=used_hint))
        for word_id, raw_rating in raw_items
    ]


async def apply_word_ratings(
    db: AsyncSession,
    user_id: int,
    ratings: Mapping[int, int] | Sequence[tuple[int, int] | WordRatingInput],
    *,
    used_hint: bool = False,
    now: datetime | None = None,
    scheduler: Scheduler | None = None,
) -> list[WordRatingResult]:
    """Apply FSRS ratings to a batch of words owned by ``user_id``.

    Validates all ratings and ``now`` before touching the database. Strictly filters
    ``UserWordFSRS`` rows by ``user_id`` so foreign or non-existent ``word_id`` values
    never modify another user's cards. Flushes updated rows without committing so the
    caller retains full transactional control.
    """
    now_utc = _resolve_now_utc(now)
    validated_entries = _normalize_ratings_batch(ratings, used_hint=used_hint)
    if not validated_entries:
        return []

    queryable_word_ids = sorted(
        {word_id for word_id, _, _ in validated_entries if 1 <= word_id <= _PG_INT32_MAX},
    )
    if not queryable_word_ids:
        return []

    stmt = (
        select(UserWordFSRS)
        .where(
            UserWordFSRS.user_id == user_id,
            UserWordFSRS.word_id.in_(queryable_word_ids),
        )
        .order_by(UserWordFSRS.word_id.asc())
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rows = (await db.execute(stmt)).scalars().all()
    cards_by_word_id = {card.word_id: card for card in rows}

    active_scheduler = scheduler if scheduler is not None else _DEFAULT_SCHEDULER
    results: list[WordRatingResult] = []
    updated_any = False

    for word_id, raw_rating, effective_rating in validated_entries:
        card_row = cards_by_word_id.get(word_id)
        if card_row is None:
            continue
        review_card(
            card_row,
            effective_rating,
            used_hint=False,
            now=now_utc,
            scheduler=active_scheduler,
        )
        updated_any = True
        assert card_row.last_review is not None
        results.append(
            WordRatingResult(
                word_id=card_row.word_id,
                raw_rating=raw_rating,
                effective_rating=int(effective_rating),
                state=card_row.state,
                stability=card_row.stability,
                difficulty=card_row.difficulty,
                due_at=card_row.due_at,
                reps=card_row.reps,
                lapses=card_row.lapses,
                last_review=card_row.last_review,
            ),
        )

    if updated_any:
        await db.flush()

    return results


__all__ = [
    "CARD_STATE_LEARNING",
    "CARD_STATE_NEW",
    "CARD_STATE_RELEARNING",
    "CARD_STATE_REVIEW",
    "HINT_MAX_RATING",
    "MAX_RATING",
    "MIN_RATING",
    "PriorityTier",
    "SelectedWord",
    "WordRatingInput",
    "WordRatingResult",
    "apply_fsrs_card_to_orm",
    "apply_hint_anticheat",
    "apply_word_ratings",
    "create_default_scheduler",
    "fsrs_card_to_orm",
    "is_new_card",
    "orm_to_fsrs_card",
    "review_card",
    "select_target_words",
]
