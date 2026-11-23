from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    """Telegram user profile, preferences, and daily LLM quota state."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("level IN ('A1', 'A2', 'B1')", name="level_valid"),
        CheckConstraint(
            "exercises_per_set >= 1 AND exercises_per_set <= 30",
            name="exercises_per_set_range",
        ),
        CheckConstraint("theme IN ('system', 'dark', 'light')", name="theme_valid"),
        CheckConstraint(
            "reminder_hour >= 0 AND reminder_hour <= 23",
            name="reminder_hour_range",
        ),
        CheckConstraint("daily_llm_calls >= 0", name="daily_llm_calls_non_negative"),
    )

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    level: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="A1",
        server_default=text("'A1'"),
    )
    exercises_per_set: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        server_default=text("5"),
    )
    theme: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="system",
        server_default=text("'system'"),
    )
    sheets_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    reminder_hour: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=19,
        server_default=text("19"),
    )
    timezone: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="Europe/Kyiv",
        server_default=text("'Europe/Kyiv'"),
    )
    daily_llm_calls: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    llm_calls_reset_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    decks: Mapped[list["Deck"]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    fsrs_cards: Mapped[list["UserWordFSRS"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    sessions: Mapped[list["TrainingSession"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    attempts: Mapped[list["Attempt"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    weekly_snapshots: Mapped[list["WeeklySnapshot"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Deck(Base):
    """Vocabulary deck belonging to a user or shared globally (owner_id IS NULL)."""

    __tablename__ = "decks"
    __table_args__ = (
        CheckConstraint("level IN ('A1', 'A2', 'B1', 'mixed')", name="level_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="mixed",
        server_default=text("'mixed'"),
    )

    owner: Mapped["User | None"] = relationship(back_populates="decks")
    words: Mapped[list["Word"]] = relationship(
        back_populates="deck",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Word(Base):
    """German vocabulary entry inside a deck."""

    __tablename__ = "words"
    __table_args__ = (
        CheckConstraint(
            "source IN ('text', 'file', 'sheets', 'system')",
            name="source_valid",
        ),
        Index(
            "uq_words_deck_id_word_de_normalized",
            "deck_id",
            text("lower(TRIM(BOTH FROM word_de))"),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deck_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("decks.id", ondelete="CASCADE"),
        nullable=False,
    )
    word_de: Mapped[str] = mapped_column(Text, nullable=False)
    translation_ua: Mapped[str] = mapped_column(Text, nullable=False)
    part_of_speech: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="text",
        server_default=text("'text'"),
    )

    deck: Mapped["Deck"] = relationship(back_populates="words")
    fsrs_cards: Mapped[list["UserWordFSRS"]] = relationship(
        back_populates="word",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class UserWordFSRS(Base):
    """Per-user FSRS scheduling card mapped to a vocabulary word."""

    __tablename__ = "user_word_fsrs"
    __table_args__ = (
        CheckConstraint("state >= 0 AND state <= 3", name="state_range"),
        CheckConstraint("stability >= 0", name="stability_non_negative"),
        CheckConstraint("difficulty >= 0", name="difficulty_non_negative"),
        CheckConstraint("reps >= 0", name="reps_non_negative"),
        CheckConstraint("lapses >= 0", name="lapses_non_negative"),
        Index("ix_user_word_fsrs_user_id_due_at", "user_id", "due_at"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        primary_key=True,
    )
    word_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("words.id", ondelete="CASCADE"),
        primary_key=True,
    )
    state: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    stability: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default=text("0.0"),
    )
    difficulty: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default=text("0.0"),
    )
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    reps: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    lapses: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    last_review: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    user: Mapped["User"] = relationship(back_populates="fsrs_cards")
    word: Mapped["Word"] = relationship(back_populates="fsrs_cards")


class TrainingSession(Base):
    """Training set session grouping a sequence of exercise attempts."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("mode IN ('mix', 'topic')", name="mode_valid"),
        CheckConstraint("total_exercises >= 1", name="total_exercises_positive"),
        CheckConstraint(
            "status IN ('active', 'completed', 'abandoned')",
            name="status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="mix",
        server_default=text("'mix'"),
    )
    target_topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_exercises: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5,
        server_default=text("5"),
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    user: Mapped["User"] = relationship(back_populates="sessions")
    attempts: Mapped[list["Attempt"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Attempt(Base):
    """Single generated exercise and optional user answer + LLM evaluation."""

    __tablename__ = "attempts"
    __table_args__ = (
        CheckConstraint(
            "exercise_type IN ('translation', 'fill_blank', 'find_error', 'combine_sentences')",
            name="exercise_type_valid",
        ),
        CheckConstraint("level IN ('A1', 'A2', 'B1')", name="level_valid"),
        CheckConstraint(
            "grammar_score >= 0 AND grammar_score <= 100",
            name="grammar_score_range",
        ),
        Index("ix_attempts_user_id_created_at_desc", "user_id", desc("created_at")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
    )
    word_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    exercise_type: Mapped[str] = mapped_column(String(32), nullable=False)
    level: Mapped[str] = mapped_column(String(8), nullable=False)
    grammar_topic: Mapped[str] = mapped_column(Text, nullable=False)
    question_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    user_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    used_hint: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    grammar_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fsrs_ratings: Mapped[dict[str, Any] | list[dict[str, Any]] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    youtube_topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    session: Mapped["TrainingSession"] = relationship(back_populates="attempts")
    user: Mapped["User"] = relationship(back_populates="attempts")


class WeeklySnapshot(Base):
    """Weekly 5-axis radar chart snapshot for progress reporting."""

    __tablename__ = "weekly_snapshots"
    __table_args__ = (
        UniqueConstraint("user_id", "week_start"),
        CheckConstraint("vocab_score >= 0 AND vocab_score <= 100", name="vocab_score_range"),
        CheckConstraint("grammar_score >= 0 AND grammar_score <= 100", name="grammar_score_range"),
        CheckConstraint(
            "consistency_score >= 0 AND consistency_score <= 100",
            name="consistency_score_range",
        ),
        CheckConstraint("writing_score >= 0 AND writing_score <= 100", name="writing_score_range"),
        CheckConstraint(
            "listening_score >= 0 AND listening_score <= 100",
            name="listening_score_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
    )
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    vocab_score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    grammar_score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    consistency_score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    writing_score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    listening_score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    user: Mapped["User"] = relationship(back_populates="weekly_snapshots")
