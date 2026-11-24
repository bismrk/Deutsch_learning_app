"""Pydantic v2 schemas for LLM exercise generation, public projections, and answer evaluation."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.schemas.users import UserLevel

ExerciseType = Literal["translation", "fill_blank", "find_error", "combine_sentences"]

EXERCISE_TYPES: tuple[ExerciseType, ...] = (
    "translation",
    "fill_blank",
    "find_error",
    "combine_sentences",
)
CEFR_LEVELS: tuple[UserLevel, ...] = ("A1", "A2", "B1")

FILL_BLANK_MARKER = "___"
MIN_GRAMMAR_SCORE = 0
MAX_GRAMMAR_SCORE = 100
MIN_FSRS_RATING = 1
MAX_FSRS_RATING = 4


def _strip_non_empty(value: str) -> str:
    """Strip surrounding whitespace and reject empty or whitespace-only strings."""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Field must be a non-empty string.")
    return cleaned


def _normalize_whitespace_for_comparison(value: str) -> str:
    """Collapse internal whitespace for semantic equality checks while preserving letter case."""
    return " ".join(value.split())


NonEmptyStr = Annotated[StrictStr, Field(min_length=1), AfterValidator(_strip_non_empty)]
PositiveWordId = Annotated[StrictInt, Field(gt=0)]


def _validate_unique_word_ids(word_ids: list[int]) -> list[int]:
    if len(set(word_ids)) != len(word_ids):
        raise ValueError("word IDs must not contain duplicates.")
    return word_ids


class TargetWord(BaseModel):
    """Target vocabulary word passed into exercise generation and evaluation prompts."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: PositiveWordId
    word_de: NonEmptyStr
    translation_ua: NonEmptyStr
    part_of_speech: StrictStr | None = None

    @field_validator("part_of_speech", mode="after")
    @classmethod
    def _normalize_part_of_speech(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class _ExerciseCommon(BaseModel):
    """Shared validated fields and invariants for public and full generated exercises."""

    exercise_type: ExerciseType
    level: UserLevel
    grammar_topic: NonEmptyStr
    target_word_ids: list[PositiveWordId]
    instruction_ua: NonEmptyStr
    task_text: NonEmptyStr

    @field_validator("target_word_ids", mode="after")
    @classmethod
    def _ensure_unique_target_word_ids(cls, value: list[int]) -> list[int]:
        return _validate_unique_word_ids(value)

    @model_validator(mode="after")
    def _validate_exercise_type_semantics(self) -> _ExerciseCommon:
        if self.exercise_type == "fill_blank" and FILL_BLANK_MARKER not in self.task_text:
            raise ValueError(
                f"fill_blank exercise task_text must contain at least one '{FILL_BLANK_MARKER}' "
                "blank marker.",
            )
        return self


class ExercisePublic(_ExerciseCommon):
    """Client-safe exercise projection that never exposes ``reference_answer`` or ``hint_ua``."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    @classmethod
    def from_generated(cls, exercise: GeneratedExercise) -> ExercisePublic:
        """Create a sanitized public projection from a ``GeneratedExercise`` instance."""
        return cls(
            exercise_type=exercise.exercise_type,
            level=exercise.level,
            grammar_topic=exercise.grammar_topic,
            target_word_ids=list(exercise.target_word_ids),
            instruction_ua=exercise.instruction_ua,
            task_text=exercise.task_text,
        )


class GeneratedExercise(_ExerciseCommon):
    """Full LLM-generated exercise payload stored server-side (includes hidden answer and hint)."""

    model_config = ConfigDict(extra="forbid")

    hint_ua: NonEmptyStr
    reference_answer: NonEmptyStr

    @model_validator(mode="after")
    def _validate_find_error_distinct_answer(self) -> GeneratedExercise:
        if self.exercise_type == "find_error":
            normalized_task = _normalize_whitespace_for_comparison(self.task_text)
            normalized_reference = _normalize_whitespace_for_comparison(self.reference_answer)
            if normalized_task == normalized_reference:
                raise ValueError(
                    "find_error exercise task_text must differ from reference_answer.",
                )
        return self

    def to_public(self) -> ExercisePublic:
        """Return a client-safe ``ExercisePublic`` projection without answer or hint."""
        return ExercisePublic.from_generated(self)


class WordRating(BaseModel):
    """Per-word FSRS rating (1–4) produced independently from overall grammar grading."""

    model_config = ConfigDict(extra="forbid")

    word_id: PositiveWordId
    word_de: NonEmptyStr
    fsrs_rating: StrictInt = Field(ge=MIN_FSRS_RATING, le=MAX_FSRS_RATING)
    note_ua: NonEmptyStr


class ExerciseError(BaseModel):
    """Single grammar or lexical error item highlighted in Ukrainian feedback."""

    model_config = ConfigDict(extra="forbid")

    wrong_part: NonEmptyStr
    correct_part: NonEmptyStr
    explanation_ua: NonEmptyStr


class ExerciseEvaluation(BaseModel):
    """Structured LLM grading output separating grammar score (0–100) from FSRS ratings (1–4)."""

    model_config = ConfigDict(extra="forbid")

    is_correct: StrictBool
    grammar_score: StrictInt = Field(ge=MIN_GRAMMAR_SCORE, le=MAX_GRAMMAR_SCORE)
    corrected_answer: NonEmptyStr
    word_ratings: list[WordRating]
    errors: list[ExerciseError]
    youtube_topic: NonEmptyStr
    summary_ua: NonEmptyStr

    @field_validator("word_ratings", mode="after")
    @classmethod
    def _ensure_unique_rated_word_ids(cls, value: list[WordRating]) -> list[WordRating]:
        _validate_unique_word_ids([item.word_id for item in value])
        return value


__all__ = [
    "CEFR_LEVELS",
    "EXERCISE_TYPES",
    "FILL_BLANK_MARKER",
    "MAX_FSRS_RATING",
    "MAX_GRAMMAR_SCORE",
    "MIN_FSRS_RATING",
    "MIN_GRAMMAR_SCORE",
    "ExerciseError",
    "ExerciseEvaluation",
    "ExercisePublic",
    "ExerciseType",
    "GeneratedExercise",
    "NonEmptyStr",
    "PositiveWordId",
    "TargetWord",
    "WordRating",
]
