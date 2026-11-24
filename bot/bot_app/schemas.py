"""Independent Pydantic v2 schemas matching the Backend HTTP API contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

UserLevel = Literal["A1", "A2", "B1"]
UserTheme = Literal["system", "dark", "light"]
WordSource = Literal["text", "file", "sheets", "system"]
ExerciseType = Literal["translation", "fill_blank", "find_error", "combine_sentences"]
SessionMode = Literal["mix", "topic"]
SessionStatus = Literal["active", "completed", "abandoned"]
RadarAxisKey = Literal["vocab", "grammar", "consistency", "writing", "listening"]
RadarAxisStatus = Literal["active", "coming_soon"]

USER_LEVELS: tuple[UserLevel, ...] = ("A1", "A2", "B1")
VALID_USER_LEVELS: frozenset[str] = frozenset(USER_LEVELS)


class _APIResponseModel(BaseModel):
    """Base model for Backend API responses tolerant of additive server fields."""

    model_config = ConfigDict(extra="ignore")


class UserProfile(_APIResponseModel):
    """User profile returned by ``GET /api/users/me`` and ``PATCH /api/users/settings``."""

    telegram_id: int
    level: UserLevel
    exercises_per_set: int
    theme: UserTheme
    sheets_url: str | None = None
    reminder_hour: int
    timezone: str
    daily_llm_calls: int
    created_at: datetime


class WordItem(_APIResponseModel):
    """Single vocabulary entry with its FSRS scheduling state."""

    id: int
    word_de: str
    translation_ua: str
    part_of_speech: str | None = None
    source: WordSource
    state: int
    stability: float
    difficulty: float
    due_at: datetime | None = None
    reps: int
    lapses: int


class WordList(_APIResponseModel):
    """Paginated vocabulary list returned by ``GET /api/words``."""

    items: list[WordItem]
    total: int


class WordImportResult(_APIResponseModel):
    """Import counters returned by ``POST /api/words/import`` and ``sync-sheets``."""

    imported: int
    duplicates: int
    skipped: int


class ExercisePublic(_APIResponseModel):
    """Public exercise payload without hidden reference answer or hint."""

    exercise_type: ExerciseType
    level: UserLevel
    grammar_topic: str
    target_word_ids: list[int]
    instruction_ua: str
    task_text: str


class WordRating(_APIResponseModel):
    """Per-word FSRS rating returned as part of answer evaluation."""

    word_id: int
    word_de: str
    fsrs_rating: int
    note_ua: str


class ExerciseError(_APIResponseModel):
    """Single highlighted error in Ukrainian answer feedback."""

    wrong_part: str
    correct_part: str
    explanation_ua: str


class ExerciseEvaluation(_APIResponseModel):
    """Structured answer evaluation returned by ``POST /api/sessions/{id}/answer``."""

    is_correct: bool
    grammar_score: int
    corrected_answer: str
    word_ratings: list[WordRating]
    errors: list[ExerciseError]
    youtube_topic: str
    summary_ua: str


class SessionSummary(_APIResponseModel):
    """Summary returned when the final exercise of a training set is completed."""

    total_exercises: int
    correct_count: int
    avg_grammar_score: float
    weak_topics: list[str]


class SessionStartResponse(_APIResponseModel):
    """Response payload for ``POST /api/sessions/start``."""

    session_id: int
    mode: SessionMode
    target_topic: str | None = None
    total_exercises: int
    completed_exercises: int
    current_exercise_number: int
    status: SessionStatus
    created_at: datetime
    attempt_id: int
    used_hint: bool
    exercise: ExercisePublic


class ActiveSessionResponse(SessionStartResponse):
    """Response payload for ``GET /api/sessions/active``."""

    hint_ua: str | None = None


class SessionHintResponse(_APIResponseModel):
    """Response payload for ``POST /api/sessions/{id}/hint``."""

    hint_ua: str
    used_hint: bool


class SessionAnswerResponse(_APIResponseModel):
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


class SessionAbandonResponse(_APIResponseModel):
    """Response payload for ``POST /api/sessions/{id}/abandon``."""

    session_id: int
    status: SessionStatus
    completed_exercises: int
    total_exercises: int


class RadarAxis(_APIResponseModel):
    """Single 0–100 axis entry on the B1-calibrated progress radar."""

    axis: RadarAxisKey
    label_ua: str
    score: int
    status: RadarAxisStatus


class WeakTopicItem(_APIResponseModel):
    """Weak grammar topic aggregated over the last 14 days."""

    grammar_topic: str
    avg_grammar_score: float
    attempts_count: int
    youtube_topic: str | None = None


class RadarStatsResponse(_APIResponseModel):
    """Response payload for ``GET /api/stats/radar``."""

    axes: list[RadarAxis]
    weak_topics: list[WeakTopicItem] = Field(default_factory=list)


__all__ = [
    "USER_LEVELS",
    "VALID_USER_LEVELS",
    "ActiveSessionResponse",
    "ExerciseError",
    "ExerciseEvaluation",
    "ExercisePublic",
    "ExerciseType",
    "RadarAxis",
    "RadarAxisKey",
    "RadarAxisStatus",
    "RadarStatsResponse",
    "SessionAbandonResponse",
    "SessionAnswerResponse",
    "SessionHintResponse",
    "SessionMode",
    "SessionStartResponse",
    "SessionStatus",
    "SessionSummary",
    "UserLevel",
    "UserProfile",
    "UserTheme",
    "WeakTopicItem",
    "WordImportResult",
    "WordItem",
    "WordList",
    "WordRating",
    "WordSource",
]
