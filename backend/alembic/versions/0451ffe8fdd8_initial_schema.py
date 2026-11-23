"""initial_schema

Revision ID: 0451ffe8fdd8
Revises:
Create Date: 2026-11-24 00:01:24.760064
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0451ffe8fdd8"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply schema changes."""
    op.create_table(
        "users",
        sa.Column("telegram_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("level", sa.String(length=8), server_default=sa.text("'A1'"), nullable=False),
        sa.Column("exercises_per_set", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column(
            "theme",
            sa.String(length=16),
            server_default=sa.text("'system'"),
            nullable=False,
        ),
        sa.Column("sheets_url", sa.Text(), nullable=True),
        sa.Column("reminder_hour", sa.Integer(), server_default=sa.text("19"), nullable=False),
        sa.Column(
            "timezone",
            sa.Text(),
            server_default=sa.text("'Europe/Kyiv'"),
            nullable=False,
        ),
        sa.Column("daily_llm_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("llm_calls_reset_date", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("level IN ('A1', 'A2', 'B1')", name=op.f("ck_users_level_valid")),
        sa.CheckConstraint(
            "theme IN ('system', 'dark', 'light')",
            name=op.f("ck_users_theme_valid"),
        ),
        sa.CheckConstraint(
            "daily_llm_calls >= 0",
            name=op.f("ck_users_daily_llm_calls_non_negative"),
        ),
        sa.CheckConstraint(
            "exercises_per_set >= 1 AND exercises_per_set <= 30",
            name=op.f("ck_users_exercises_per_set_range"),
        ),
        sa.CheckConstraint(
            "reminder_hour >= 0 AND reminder_hour <= 23",
            name=op.f("ck_users_reminder_hour_range"),
        ),
        sa.PrimaryKeyConstraint("telegram_id", name=op.f("pk_users")),
    )
    op.create_table(
        "decks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("level", sa.String(length=16), server_default=sa.text("'mixed'"), nullable=False),
        sa.CheckConstraint(
            "level IN ('A1', 'A2', 'B1', 'mixed')",
            name=op.f("ck_decks_level_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.telegram_id"],
            name=op.f("fk_decks_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decks")),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.String(length=16), server_default=sa.text("'mix'"), nullable=False),
        sa.Column("target_topic", sa.Text(), nullable=True),
        sa.Column("total_exercises", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("mode IN ('mix', 'topic')", name=op.f("ck_sessions_mode_valid")),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'abandoned')",
            name=op.f("ck_sessions_status_valid"),
        ),
        sa.CheckConstraint(
            "total_exercises >= 1",
            name=op.f("ck_sessions_total_exercises_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_id"],
            name=op.f("fk_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
    )
    op.create_table(
        "weekly_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("vocab_score", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("grammar_score", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("consistency_score", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("writing_score", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("listening_score", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "consistency_score >= 0 AND consistency_score <= 100",
            name=op.f("ck_weekly_snapshots_consistency_score_range"),
        ),
        sa.CheckConstraint(
            "grammar_score >= 0 AND grammar_score <= 100",
            name=op.f("ck_weekly_snapshots_grammar_score_range"),
        ),
        sa.CheckConstraint(
            "listening_score >= 0 AND listening_score <= 100",
            name=op.f("ck_weekly_snapshots_listening_score_range"),
        ),
        sa.CheckConstraint(
            "vocab_score >= 0 AND vocab_score <= 100",
            name=op.f("ck_weekly_snapshots_vocab_score_range"),
        ),
        sa.CheckConstraint(
            "writing_score >= 0 AND writing_score <= 100",
            name=op.f("ck_weekly_snapshots_writing_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_id"],
            name=op.f("fk_weekly_snapshots_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_weekly_snapshots")),
        sa.UniqueConstraint(
            "user_id",
            "week_start",
            name=op.f("uq_weekly_snapshots_user_id_week_start"),
        ),
    )
    op.create_table(
        "attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("word_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("exercise_type", sa.String(length=32), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("grammar_topic", sa.Text(), nullable=False),
        sa.Column("question_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("user_answer", sa.Text(), nullable=True),
        sa.Column("used_hint", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=True),
        sa.Column("grammar_score", sa.Integer(), nullable=True),
        sa.Column("fsrs_ratings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("youtube_topic", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "exercise_type IN ('translation', 'fill_blank', 'find_error', 'combine_sentences')",
            name=op.f("ck_attempts_exercise_type_valid"),
        ),
        sa.CheckConstraint("level IN ('A1', 'A2', 'B1')", name=op.f("ck_attempts_level_valid")),
        sa.CheckConstraint(
            "grammar_score >= 0 AND grammar_score <= 100",
            name=op.f("ck_attempts_grammar_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_attempts_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_id"],
            name=op.f("fk_attempts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attempts")),
    )
    op.create_index(
        "ix_attempts_user_id_created_at_desc",
        "attempts",
        ["user_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "words",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("deck_id", sa.Integer(), nullable=False),
        sa.Column("word_de", sa.Text(), nullable=False),
        sa.Column("translation_ua", sa.Text(), nullable=False),
        sa.Column("part_of_speech", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), server_default=sa.text("'text'"), nullable=False),
        sa.CheckConstraint(
            "source IN ('text', 'file', 'sheets', 'system')",
            name=op.f("ck_words_source_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["deck_id"],
            ["decks.id"],
            name=op.f("fk_words_deck_id_decks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_words")),
    )
    op.create_index(
        "uq_words_deck_id_word_de_normalized",
        "words",
        ["deck_id", sa.literal_column("lower(TRIM(BOTH FROM word_de))")],
        unique=True,
    )
    op.create_table(
        "user_word_fsrs",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("word_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("stability", sa.Float(), server_default=sa.text("0.0"), nullable=False),
        sa.Column("difficulty", sa.Float(), server_default=sa.text("0.0"), nullable=False),
        sa.Column(
            "due_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reps", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lapses", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_review", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "difficulty >= 0",
            name=op.f("ck_user_word_fsrs_difficulty_non_negative"),
        ),
        sa.CheckConstraint(
            "lapses >= 0",
            name=op.f("ck_user_word_fsrs_lapses_non_negative"),
        ),
        sa.CheckConstraint("reps >= 0", name=op.f("ck_user_word_fsrs_reps_non_negative")),
        sa.CheckConstraint(
            "stability >= 0",
            name=op.f("ck_user_word_fsrs_stability_non_negative"),
        ),
        sa.CheckConstraint(
            "state >= 0 AND state <= 3",
            name=op.f("ck_user_word_fsrs_state_range"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_id"],
            name=op.f("fk_user_word_fsrs_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["word_id"],
            ["words.id"],
            name=op.f("fk_user_word_fsrs_word_id_words"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "word_id", name=op.f("pk_user_word_fsrs")),
    )
    op.create_index(
        "ix_user_word_fsrs_user_id_due_at",
        "user_word_fsrs",
        ["user_id", "due_at"],
        unique=False,
    )


def downgrade() -> None:
    """Revert schema changes."""
    op.drop_index("ix_user_word_fsrs_user_id_due_at", table_name="user_word_fsrs")
    op.drop_table("user_word_fsrs")
    op.drop_index("uq_words_deck_id_word_de_normalized", table_name="words")
    op.drop_table("words")
    op.drop_index("ix_attempts_user_id_created_at_desc", table_name="attempts")
    op.drop_table("attempts")
    op.drop_table("weekly_snapshots")
    op.drop_table("sessions")
    op.drop_table("decks")
    op.drop_table("users")
