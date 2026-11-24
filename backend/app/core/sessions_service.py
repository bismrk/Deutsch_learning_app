"""Business logic helpers for training session orchestration, scheduling, and summaries."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.fsrs_engine import (
    SelectedWord,
    WordRatingInput,
    apply_hint_anticheat,
    select_target_words,
)
from app.core.llm_service import LLMService
from app.core.prompts import (
    build_evaluation_prompts,
    build_generation_prompts,
    get_grammar_topics,
)
from app.db.models import Attempt, Deck, Word
from app.schemas.exercises import (
    EXERCISE_TYPES,
    ExerciseEvaluation,
    ExerciseType,
    GeneratedExercise,
    TargetWord,
    WordRating,
)
from app.schemas.sessions import SessionMode, SessionSummary
from app.schemas.users import UserLevel

TARGET_WORDS_PER_EXERCISE = 3
WEAK_TOPIC_SCORE_THRESHOLD = 80
MAX_WEAK_TOPICS = 3


def choose_next_exercise_type(previous_types: Sequence[str]) -> ExerciseType:
    """Select the next exercise type evenly across all 4 types without consecutive repeats."""
    counts: dict[ExerciseType, int] = dict.fromkeys(EXERCISE_TYPES, 0)
    for item in previous_types:
        if item in counts:
            counts[item] += 1  # type: ignore[index]

    last_type = previous_types[-1] if previous_types else None
    min_count = min(counts.values())

    candidates = [
        ex_type
        for ex_type in EXERCISE_TYPES
        if counts[ex_type] == min_count and ex_type != last_type
    ]
    if candidates:
        return candidates[0]

    non_repeating = [ex_type for ex_type in EXERCISE_TYPES if ex_type != last_type]
    return min(non_repeating, key=lambda ex_type: counts[ex_type])


def choose_grammar_topic(
    *,
    level: UserLevel,
    mode: SessionMode,
    target_topic: str | None,
    previous_topics: Sequence[str],
) -> str:
    """Choose the grammar topic for the next exercise according to session mode and level."""
    if mode == "topic":
        cleaned = target_topic.strip() if isinstance(target_topic, str) else ""
        if not cleaned:
            raise ValueError("target_topic must be non-empty when mode is 'topic'.")
        return cleaned

    catalog = get_grammar_topics(level)
    counts: dict[str, int] = dict.fromkeys(catalog, 0)
    for topic in previous_topics:
        if topic in counts:
            counts[topic] += 1

    last_topic = previous_topics[-1] if previous_topics else None
    min_count = min(counts.values())

    candidates = [
        topic for topic in catalog if counts[topic] == min_count and topic != last_topic
    ]
    if candidates:
        return candidates[0]

    return min(catalog, key=lambda topic: counts[topic])


def collect_used_word_ids(attempts: Sequence[Attempt]) -> set[int]:
    """Collect all unique positive word IDs already used across a session's attempts."""
    used: set[int] = set()
    for attempt in attempts:
        if isinstance(attempt.word_ids, list):
            for wid in attempt.word_ids:
                if isinstance(wid, int) and not isinstance(wid, bool) and wid > 0:
                    used.add(wid)
    return used


async def select_words_for_exercise(
    db: AsyncSession,
    user_id: int,
    *,
    exclude_word_ids: Collection[int] | None = None,
    count: int = TARGET_WORDS_PER_EXERCISE,
    now: datetime,
) -> list[SelectedWord]:
    """Select 1–3 FSRS-prioritized target words, falling back to full dictionary when exhausted."""
    if exclude_word_ids:
        selected = await select_target_words(
            db,
            user_id,
            count=count,
            exclude_word_ids=exclude_word_ids,
            now=now,
        )
        if selected:
            return selected

    return await select_target_words(
        db,
        user_id,
        count=count,
        exclude_word_ids=None,
        now=now,
    )


async def load_attempt_target_words(
    db: AsyncSession,
    *,
    user_id: int,
    word_ids: Sequence[int],
) -> list[TargetWord]:
    """Load owned vocabulary words referenced by an attempt in their original order."""
    valid_ids = [
        wid for wid in word_ids if isinstance(wid, int) and not isinstance(wid, bool) and wid > 0
    ]
    if not valid_ids:
        return []

    stmt = (
        select(Word)
        .join(Deck, Word.deck_id == Deck.id)
        .where(Deck.owner_id == user_id, Word.id.in_(valid_ids))
    )
    rows = (await db.execute(stmt)).scalars().all()
    words_by_id = {word.id: word for word in rows}

    ordered: list[TargetWord] = []
    seen: set[int] = set()
    for wid in valid_ids:
        if wid in seen:
            continue
        word = words_by_id.get(wid)
        if word is None:
            continue
        seen.add(wid)
        ordered.append(
            TargetWord(
                id=word.id,
                word_de=word.word_de,
                translation_ua=word.translation_ua,
                part_of_speech=word.part_of_speech,
            ),
        )
    return ordered


def _selected_words_to_targets(selected_words: Sequence[SelectedWord]) -> list[TargetWord]:
    return [
        TargetWord(
            id=item.word_id,
            word_de=item.word_de,
            translation_ua=item.translation_ua,
            part_of_speech=item.part_of_speech,
        )
        for item in selected_words
    ]


async def generate_exercise_for_session(
    llm_service: LLMService,
    *,
    exercise_type: ExerciseType,
    level: UserLevel,
    grammar_topic: str,
    selected_words: Sequence[SelectedWord],
) -> tuple[GeneratedExercise, list[int]]:
    """Build prompts, invoke ``LLMService``, and reconcile ``target_word_ids`` to owned words."""
    target_words = _selected_words_to_targets(selected_words)
    system_prompt, user_prompt = build_generation_prompts(
        exercise_type=exercise_type,
        level=level,
        grammar_topic=grammar_topic,
        target_words=target_words,
    )
    generated = await llm_service.generate_structured(
        response_model=GeneratedExercise,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    if selected_words:
        allowed_ids = {word.word_id for word in selected_words}
        reconciled_ids = [wid for wid in generated.target_word_ids if wid in allowed_ids]
        if not reconciled_ids:
            reconciled_ids = [word.word_id for word in selected_words]
    else:
        reconciled_ids = []

    if reconciled_ids != generated.target_word_ids:
        generated = generated.model_copy(update={"target_word_ids": reconciled_ids})

    return generated, reconciled_ids


async def evaluate_attempt_answer(
    llm_service: LLMService,
    *,
    exercise: GeneratedExercise,
    user_answer: str,
    target_words: Sequence[TargetWord],
    attempt_word_ids: Sequence[int],
    used_hint: bool,
) -> tuple[ExerciseEvaluation, list[WordRatingInput]]:
    """Evaluate a user's answer via ``LLMService``, filtering word IDs and applying hint cap.

    Returns ``(effective_evaluation, raw_rating_inputs)`` where:
    - ``effective_evaluation`` has ``word_ratings`` filtered to ``attempt_word_ids`` and capped
      via ``min(rating, 2)`` when ``used_hint=True``;
    - ``raw_rating_inputs`` contains the filtered raw ratings to pass into
      ``fsrs_engine.apply_word_ratings(..., used_hint=used_hint)``.
    """
    system_prompt, user_prompt = build_evaluation_prompts(
        exercise=exercise,
        user_answer=user_answer,
        target_words=target_words,
    )
    raw_evaluation = await llm_service.generate_structured(
        response_model=ExerciseEvaluation,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    allowed_word_ids = {
        wid
        for wid in attempt_word_ids
        if isinstance(wid, int) and not isinstance(wid, bool) and wid > 0
    }
    raw_inputs: list[WordRatingInput] = []
    effective_word_ratings: list[WordRating] = []

    for rating_item in raw_evaluation.word_ratings:
        if rating_item.word_id not in allowed_word_ids:
            continue
        raw_inputs.append(
            WordRatingInput(word_id=rating_item.word_id, rating=rating_item.fsrs_rating),
        )
        effective_rating = int(
            apply_hint_anticheat(rating_item.fsrs_rating, used_hint=used_hint),
        )
        effective_word_ratings.append(
            rating_item.model_copy(update={"fsrs_rating": effective_rating}),
        )

    effective_evaluation = raw_evaluation.model_copy(
        update={"word_ratings": effective_word_ratings},
    )
    return effective_evaluation, raw_inputs


def build_session_summary(completed_attempts: Sequence[Attempt]) -> SessionSummary:
    """Aggregate final set summary metrics and weak grammar topics from completed attempts."""
    total = len(completed_attempts)
    correct_count = sum(1 for attempt in completed_attempts if attempt.is_correct is True)
    scores = [
        attempt.grammar_score
        for attempt in completed_attempts
        if attempt.grammar_score is not None
    ]
    avg_grammar_score = round(sum(scores) / len(scores), 1) if scores else 0.0

    topic_scores: dict[str, list[int]] = {}
    first_seen_order: dict[str, int] = {}

    for index, attempt in enumerate(completed_attempts):
        is_weak = attempt.is_correct is False or (
            attempt.grammar_score is not None
            and attempt.grammar_score < WEAK_TOPIC_SCORE_THRESHOLD
        )
        if not is_weak:
            continue
        topic = attempt.grammar_topic.strip()
        if not topic:
            continue
        if topic not in topic_scores:
            topic_scores[topic] = []
            first_seen_order[topic] = index
        topic_scores[topic].append(
            attempt.grammar_score if attempt.grammar_score is not None else 0,
        )

    ranked_topics = sorted(
        topic_scores.keys(),
        key=lambda topic: (
            sum(topic_scores[topic]) / len(topic_scores[topic]),
            -len(topic_scores[topic]),
            first_seen_order[topic],
        ),
    )

    return SessionSummary(
        total_exercises=total,
        correct_count=correct_count,
        avg_grammar_score=avg_grammar_score,
        weak_topics=ranked_topics[:MAX_WEAK_TOPICS],
    )


__all__ = [
    "MAX_WEAK_TOPICS",
    "TARGET_WORDS_PER_EXERCISE",
    "WEAK_TOPIC_SCORE_THRESHOLD",
    "build_session_summary",
    "choose_grammar_topic",
    "choose_next_exercise_type",
    "collect_used_word_ids",
    "evaluate_attempt_answer",
    "generate_exercise_for_session",
    "load_attempt_target_words",
    "select_words_for_exercise",
]
