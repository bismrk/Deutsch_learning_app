"""Pydantic v2 request and response schemas for training session endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.schemas.exercises import ExerciseEvaluation, ExercisePublic

SessionMode = Literal["mix", "topic"]
SessionStatus = Literal["active", "completed", "abandoned"]

MIN_TOTAL_EXERCISES = 1
MAX_TOTAL_EXERCISES = 30
MAX_TARGET_TOPIC_LENGTH = 200
MAX_USER_ANSWER_LENGTH = 2000


def _strip_non_empty_answer(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("user_answer must be a non-empty string.")
    return cleaned


UserAnswerStr = Annotated[
    StrictStr,
    Field(min_length=1, max_length=MAX_USER_ANSWER_LENGTH),
    AfterValidator(_strip_non_empty_answer),
]


class SessionStartRequest(BaseModel):
    """Request payload for ``POST /api/sessions/start``."""

    model_config = ConfigDict(extra="forbid")

    mode: SessionMode = "mix"
    target_topic: StrictStr | None = None
    total_exercises: StrictInt | None = Field(
        default=None,
        ge=MIN_TOTAL_EXERCISES,
        le=MAX_TOTAL_EXERCISES,
    )

    @field_validator("target_topic", mode="after")
    @classmethod
    def _normalize_target_topic(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if len(cleaned) > MAX_TARGET_TOPIC_LENGTH:
            raise ValueError(
                f"target_topic must be at most {MAX_TARGET_TOPIC_LENGTH} characters.",
            )
        return cleaned

    @model_validator(mode="after")
    def _validate_mode_and_topic(self) -> SessionStartRequest:
        if self.mode == "topic" and not self.target_topic:
            raise ValueError("target_topic is required when mode is 'topic'.")
        if self.mode == "mix":
            self.target_topic = None
        return self


class SessionStartResponse(BaseModel):
    """Response payload for ``POST /api/sessions/start`` (never exposes answer or hint)."""

    session_id: int
    mode: SessionMode
    target_topic: str | None
    total_exercises: int
    completed_exercises: int
    current_exercise_number: int
    status: SessionStatus
    created_at: datetime
    attempt_id: int
    used_hint: bool
    exercise: ExercisePublic


class ActiveSessionResponse(BaseModel):
    """Response payload for ``GET /api/sessions/active`` (includes ``hint_ua`` only after hint)."""

    session_id: int
    mode: SessionMode
    target_topic: str | None
    total_exercises: int
    completed_exercises: int
    current_exercise_number: int
    status: SessionStatus
    created_at: datetime
    attempt_id: int
    used_hint: bool
    exercise: ExercisePublic
    hint_ua: str | None = None


class SessionHintResponse(BaseModel):
    """Response payload for ``POST /api/sessions/{id}/hint`` (never exposes reference answer)."""

    hint_ua: str
    used_hint: Literal[True] = True


class SessionAnswerRequest(BaseModel):
    """Request payload for ``POST /api/sessions/{id}/answer``."""

    model_config = ConfigDict(extra="forbid")

    user_answer: UserAnswerStr


class SessionSummary(BaseModel):
    """Aggregate summary returned once the final exercise of a training set is completed."""

    total_exercises: int
    correct_count: int
    avg_grammar_score: float
    weak_topics: list[str]


class SessionAnswerResponse(BaseModel):
    """Response payload for ``POST /api/sessions/{id}/answer``."""

    session_id: int
    attempt_id: int
    status: SessionStatus
    completed_exercises: int
    total_exercises: int
    used_hint: bool
    evaluation: ExerciseEvaluation
    next_attempt_id: int | None = None
    next_exercise: ExercisePublic | None = None
    summary: SessionSummary | None = None


class SessionAbandonResponse(BaseModel):
    """Response payload for ``POST /api/sessions/{id}/abandon``."""

    session_id: int
    status: Literal["abandoned"] = "abandoned"
    completed_exercises: int
    total_exercises: int


__all__ = [
    "MAX_TARGET_TOPIC_LENGTH",
    "MAX_TOTAL_EXERCISES",
    "MAX_USER_ANSWER_LENGTH",
    "MIN_TOTAL_EXERCISES",
    "ActiveSessionResponse",
    "SessionAbandonResponse",
    "SessionAnswerRequest",
    "SessionAnswerResponse",
    "SessionHintResponse",
    "SessionMode",
    "SessionStartRequest",
    "SessionStartResponse",
    "SessionStatus",
    "SessionSummary",
]
