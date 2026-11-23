import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.base import (
    NAMING_CONVENTION,
    Base,
    create_async_db_engine,
    create_session_factory,
    dispose_engine,
    get_db,
    get_engine,
    get_session_factory,
    normalize_async_database_url,
)
from app.db.models import (
    Attempt,
    Deck,
    TrainingSession,
    User,
    UserWordFSRS,
    WeeklySnapshot,
    Word,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent

FIXED_UTC_1 = datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC)
FIXED_UTC_2 = datetime(2026, 3, 2, 18, 30, 0, tzinfo=UTC)
FIXED_WEEK_START = date(2026, 3, 2)
LARGE_TELEGRAM_ID_A = 5_123_456_789  # > 2^31 - 1 (64-bit BIGINT)
LARGE_TELEGRAM_ID_B = 6_987_654_321


# ---------------------------------------------------------------------------
# Offline unit tests (no PostgreSQL connection required)
# ---------------------------------------------------------------------------


def test_settings_test_database_url_defaults_and_env_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    default_settings = Settings(_env_file=None)
    assert default_settings.test_database_url is None

    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://postgres:testpass@127.0.0.1:5432/deutsch_learning_test",
    )
    overridden = Settings(_env_file=None)
    assert (
        overridden.test_database_url
        == "postgresql+asyncpg://postgres:testpass@127.0.0.1:5432/deutsch_learning_test"
    )


@pytest.mark.parametrize(
    ("raw_url", "expected_url"),
    [
        (
            "postgresql://postgres:pass@127.0.0.1:5432/deutsch_learning",
            "postgresql+asyncpg://postgres:pass@127.0.0.1:5432/deutsch_learning",
        ),
        (
            "postgresql+asyncpg://postgres:pass@postgres:5432/deutsch_learning",
            "postgresql+asyncpg://postgres:pass@postgres:5432/deutsch_learning",
        ),
        (
            "  POSTGRESQL://user:p%40ss@localhost:5432/app?ssl=require  ",
            "postgresql+asyncpg://user:p%40ss@localhost:5432/app?ssl=require",
        ),
        (
            "postgresql://user:pass@[::1]:5432/deutsch_learning_test",
            "postgresql+asyncpg://user:pass@[::1]:5432/deutsch_learning_test",
        ),
    ],
)
def test_normalize_async_database_url_accepts_supported_schemes(
    raw_url: str,
    expected_url: str,
) -> None:
    assert normalize_async_database_url(raw_url) == expected_url


@pytest.mark.parametrize(
    "invalid_url",
    [
        None,
        "",
        "   ",
        "sqlite:///local.db",
        "sqlite+aiosqlite:///:memory:",
        "mysql://user:secret_pwd@localhost:3306/db",
        "postgresql+psycopg://user:secret_pwd@localhost:5432/db",
        "postgresql+psycopg2://user:secret_pwd@localhost:5432/db",
        "postgresql://",
        "postgresql://user:secret_pwd@localhost:notaport/db",
        "postgresql://user:secret_pwd@localhost:0/db",
        "postgresql://user:secret_pwd@localhost:70000/db",
        "postgresql://user:secret_pwd@localhost:5432/db with spaces",
    ],
)
def test_normalize_async_database_url_rejects_invalid_values_without_leaking_secrets(
    invalid_url: str | None,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        normalize_async_database_url(invalid_url)

    assert "secret_pwd" not in str(exc_info.value)


def test_importing_db_modules_does_not_open_network_connection() -> None:
    script = (
        "import os, asyncpg\n"
        "os.environ.pop('DATABASE_URL', None)\n"
        "os.environ.pop('TEST_DATABASE_URL', None)\n"
        "async def _forbid(*args, **kwargs):\n"
        "    raise AssertionError('asyncpg.connect must not be called at import time')\n"
        "asyncpg.connect = _forbid\n"
        "import app.db\n"
        "import app.db.base\n"
        "import app.db.models\n"
        "assert len(app.db.Base.metadata.tables) == 7\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"STDERR:\n{result.stderr}\nSTDOUT:\n{result.stdout}"


def test_base_metadata_registers_all_seven_tables_and_naming_convention() -> None:
    assert set(Base.metadata.tables.keys()) == {
        "users",
        "decks",
        "words",
        "user_word_fsrs",
        "sessions",
        "attempts",
        "weekly_snapshots",
    }
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    assert set(NAMING_CONVENTION.keys()) == {"ix", "uq", "ck", "fk", "pk"}

    # Verify JSONB columns and TIMESTAMPTZ columns at ORM metadata level
    attempts_table = Base.metadata.tables["attempts"]
    assert isinstance(attempts_table.c.word_ids.type, JSONB)
    assert isinstance(attempts_table.c.question_payload.type, JSONB)
    assert isinstance(attempts_table.c.fsrs_ratings.type, JSONB)

    for table_name, col_name in (
        ("users", "created_at"),
        ("user_word_fsrs", "due_at"),
        ("user_word_fsrs", "last_review"),
        ("sessions", "created_at"),
        ("attempts", "created_at"),
    ):
        col = Base.metadata.tables[table_name].c[col_name]
        assert getattr(col.type, "timezone", False) is True


async def test_engine_and_session_factory_configuration_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    await dispose_engine()
    try:
        for url in (
            "postgresql://postgres:secret@127.0.0.1:5432/deutsch_learning",
            "postgresql+asyncpg://postgres:secret@127.0.0.1:5432/deutsch_learning",
        ):
            monkeypatch.setenv("DATABASE_URL", url)
            get_settings.cache_clear()

            engine = get_engine()
            assert engine.url.drivername == "postgresql+asyncpg"

            factory = get_session_factory()
            assert factory.kw["expire_on_commit"] is False
            await dispose_engine()

        standalone_engine = create_async_db_engine(
            "postgresql://postgres:secret@127.0.0.1:5432/deutsch_learning",
        )
        try:
            standalone_factory = create_session_factory(standalone_engine)
            assert standalone_factory.kw["expire_on_commit"] is False
        finally:
            await standalone_engine.dispose()
    finally:
        await dispose_engine()
        get_settings.cache_clear()


async def test_get_db_yields_session_and_rolls_back_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            events.append("enter")
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            events.append("exit")

        async def rollback(self) -> None:
            events.append("rollback")

    monkeypatch.setattr("app.db.base.get_session_factory", lambda: FakeSession)

    # Normal flow
    gen = get_db()
    session = await anext(gen)
    assert isinstance(session, FakeSession)
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    assert events == ["enter", "exit"]

    # Error flow rolls back before closing
    events.clear()
    gen_err = get_db()
    await anext(gen_err)
    with pytest.raises(RuntimeError, match="boom"):
        await gen_err.athrow(RuntimeError("boom"))
    assert events == ["enter", "rollback", "exit"]


# ---------------------------------------------------------------------------
# PostgreSQL integration tests (isolated TEST_DATABASE_URL + rollback)
# ---------------------------------------------------------------------------


async def test_postgres_column_types_match_specification(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            """
            SELECT table_name, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
            """,
        ),
    )
    columns = {
        (row.table_name, row.column_name): (row.data_type, row.is_nullable) for row in result
    }

    # All 5 timestamp columns must be TIMESTAMPTZ
    for table_name, col_name in (
        ("users", "created_at"),
        ("user_word_fsrs", "due_at"),
        ("user_word_fsrs", "last_review"),
        ("sessions", "created_at"),
        ("attempts", "created_at"),
    ):
        assert columns[(table_name, col_name)][0] == "timestamp with time zone"

    # 64-bit BIGINT for Telegram user IDs and their FKs
    for table_name, col_name in (
        ("users", "telegram_id"),
        ("decks", "owner_id"),
        ("user_word_fsrs", "user_id"),
        ("sessions", "user_id"),
        ("attempts", "user_id"),
        ("weekly_snapshots", "user_id"),
    ):
        assert columns[(table_name, col_name)][0] == "bigint"

    # JSONB fields on attempts
    for col_name in ("word_ids", "question_payload", "fsrs_ratings"):
        assert columns[("attempts", col_name)][0] == "jsonb"

    # DATE fields
    assert columns[("users", "llm_calls_reset_date")] == ("date", "YES")
    assert columns[("weekly_snapshots", "week_start")] == ("date", "NO")

    # Required nullable fields
    assert columns[("decks", "owner_id")][1] == "YES"
    assert columns[("words", "part_of_speech")][1] == "YES"
    assert columns[("user_word_fsrs", "last_review")][1] == "YES"
    assert columns[("sessions", "target_topic")][1] == "YES"
    for col_name in (
        "user_answer",
        "is_correct",
        "grammar_score",
        "fsrs_ratings",
        "youtube_topic",
    ):
        assert columns[("attempts", col_name)][1] == "YES"

    # Phase 2 pgvector column must NOT exist in MVP schema
    assert ("words", "embedding") not in columns


async def test_get_db_executes_select_1_against_postgres(
    migrated_test_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_test_db_url)
    get_settings.cache_clear()
    await dispose_engine()
    try:
        async for session in get_db():
            value = await session.scalar(text("SELECT 1"))
            assert value == 1
    finally:
        await dispose_engine()
        get_settings.cache_clear()


async def test_users_crud_defaults_and_64bit_telegram_id(db_session: AsyncSession) -> None:
    user = User(telegram_id=LARGE_TELEGRAM_ID_A)
    db_session.add(user)
    await db_session.commit()

    # expire_on_commit=False allows reading attributes immediately after commit without IO
    assert user.telegram_id == LARGE_TELEGRAM_ID_A
    assert user.level == "A1"

    fetched = await db_session.get(User, LARGE_TELEGRAM_ID_A)
    assert fetched is not None
    assert fetched.telegram_id == LARGE_TELEGRAM_ID_A
    assert fetched.level == "A1"
    assert fetched.exercises_per_set == 5
    assert fetched.theme == "system"
    assert fetched.sheets_url is None
    assert fetched.reminder_hour == 19
    assert fetched.timezone == "Europe/Kyiv"
    assert fetched.daily_llm_calls == 0
    assert fetched.llm_calls_reset_date is None
    assert fetched.created_at is not None
    assert fetched.created_at.tzinfo is not None

    # Update
    fetched.level = "B1"
    fetched.exercises_per_set = 10
    fetched.theme = "dark"
    fetched.sheets_url = "https://docs.google.com/spreadsheets/d/example/edit"
    fetched.reminder_hour = 21
    fetched.timezone = "Europe/Berlin"
    fetched.daily_llm_calls = 7
    fetched.llm_calls_reset_date = date(2026, 3, 1)
    await db_session.commit()

    refreshed = await db_session.get(User, LARGE_TELEGRAM_ID_A)
    assert refreshed is not None
    assert refreshed.level == "B1"
    assert refreshed.exercises_per_set == 10
    assert refreshed.theme == "dark"
    assert refreshed.reminder_hour == 21
    assert refreshed.daily_llm_calls == 7
    assert refreshed.llm_calls_reset_date == date(2026, 3, 1)

    # Delete
    await db_session.delete(refreshed)
    await db_session.commit()
    assert await db_session.get(User, LARGE_TELEGRAM_ID_A) is None


async def test_decks_crud_personal_and_shared_null_owner(db_session: AsyncSession) -> None:
    user = User(telegram_id=LARGE_TELEGRAM_ID_A)
    db_session.add(user)
    await db_session.flush()

    personal_deck = Deck(owner_id=user.telegram_id, title="Мій словник", level="mixed")
    shared_deck = Deck(owner_id=None, title="Goethe A1 Starter", level="A1")
    db_session.add_all([personal_deck, shared_deck])
    await db_session.commit()

    fetched_personal = await db_session.get(Deck, personal_deck.id)
    fetched_shared = await db_session.get(Deck, shared_deck.id)
    assert fetched_personal is not None
    assert fetched_personal.owner_id == LARGE_TELEGRAM_ID_A
    assert fetched_shared is not None
    assert fetched_shared.owner_id is None
    assert fetched_shared.level == "A1"

    fetched_shared.title = "Goethe A2 Core"
    fetched_shared.level = "A2"
    await db_session.commit()

    updated_shared = await db_session.get(Deck, shared_deck.id)
    assert updated_shared is not None
    assert updated_shared.title == "Goethe A2 Core"
    assert updated_shared.level == "A2"

    await db_session.delete(updated_shared)
    await db_session.commit()
    assert await db_session.get(Deck, shared_deck.id) is None


async def test_words_crud_and_all_valid_sources(db_session: AsyncSession) -> None:
    user = User(telegram_id=LARGE_TELEGRAM_ID_A)
    db_session.add(user)
    await db_session.flush()

    deck = Deck(owner_id=user.telegram_id, title="Основна колода", level="B1")
    db_session.add(deck)
    await db_session.flush()

    words = [
        Word(
            deck_id=deck.id,
            word_de="der Zug",
            translation_ua="потяг",
            part_of_speech="noun",
            source="text",
        ),
        Word(
            deck_id=deck.id,
            word_de="die Verspätung",
            translation_ua="затримка",
            part_of_speech=None,
            source="file",
        ),
        Word(
            deck_id=deck.id,
            word_de="das Treffen",
            translation_ua="зустріч",
            source="sheets",
        ),
        Word(
            deck_id=deck.id,
            word_de="obwohl",
            translation_ua="хоча",
            part_of_speech="conjunction",
            source="system",
        ),
    ]
    db_session.add_all(words)
    await db_session.commit()

    fetched = await db_session.get(Word, words[1].id)
    assert fetched is not None
    assert fetched.part_of_speech is None
    assert fetched.source == "file"

    fetched.part_of_speech = "noun"
    fetched.translation_ua = "запізнення"
    await db_session.commit()

    updated = await db_session.get(Word, words[1].id)
    assert updated is not None
    assert updated.part_of_speech == "noun"
    assert updated.translation_ua == "запізнення"

    await db_session.delete(updated)
    await db_session.commit()
    assert await db_session.get(Word, words[1].id) is None


async def test_user_word_fsrs_crud_and_cascade_delete_on_word(
    db_session: AsyncSession,
) -> None:
    user = User(telegram_id=LARGE_TELEGRAM_ID_A)
    db_session.add(user)
    await db_session.flush()

    deck = Deck(owner_id=user.telegram_id, title="FSRS Deck", level="A2")
    db_session.add(deck)
    await db_session.flush()

    word_orm = Word(deck_id=deck.id, word_de="der Tisch", translation_ua="стіл", source="text")
    word_sql = Word(deck_id=deck.id, word_de="der Stuhl", translation_ua="стілець", source="text")
    db_session.add_all([word_orm, word_sql])
    await db_session.flush()

    card_orm = UserWordFSRS(
        user_id=user.telegram_id,
        word_id=word_orm.id,
        due_at=FIXED_UTC_1,
    )
    card_sql = UserWordFSRS(
        user_id=user.telegram_id,
        word_id=word_sql.id,
        due_at=FIXED_UTC_1,
    )
    db_session.add_all([card_orm, card_sql])
    await db_session.commit()

    fetched = await db_session.get(UserWordFSRS, (user.telegram_id, word_orm.id))
    assert fetched is not None
    assert fetched.state == 0
    assert fetched.stability == 0.0
    assert fetched.difficulty == 0.0
    assert fetched.reps == 0
    assert fetched.lapses == 0
    assert fetched.last_review is None
    assert fetched.due_at == FIXED_UTC_1
    assert fetched.due_at.tzinfo is not None

    # Update card after review
    fetched.state = 2
    fetched.stability = 14.5
    fetched.difficulty = 5.2
    fetched.reps = 3
    fetched.lapses = 1
    fetched.last_review = FIXED_UTC_1
    fetched.due_at = FIXED_UTC_2
    await db_session.commit()

    updated = await db_session.get(UserWordFSRS, (user.telegram_id, word_orm.id))
    assert updated is not None
    assert updated.state == 2
    assert updated.stability == pytest.approx(14.5)
    assert updated.last_review == FIXED_UTC_1
    assert updated.due_at == FIXED_UTC_2

    word_orm_id = word_orm.id
    word_sql_id = word_sql.id

    # 1) ORM delete of word must cascade delete UserWordFSRS
    await db_session.delete(word_orm)
    await db_session.commit()
    db_session.expire_all()
    assert await db_session.get(UserWordFSRS, (LARGE_TELEGRAM_ID_A, word_orm_id)) is None

    # 2) Direct SQL DELETE on words must also trigger PostgreSQL ON DELETE CASCADE
    await db_session.execute(delete(Word).where(Word.id == word_sql_id))
    await db_session.commit()
    db_session.expire_all()
    assert await db_session.get(UserWordFSRS, (LARGE_TELEGRAM_ID_A, word_sql_id)) is None


async def test_sessions_and_attempts_crud_unfinished_and_evaluated_jsonb(
    db_session: AsyncSession,
) -> None:
    user = User(telegram_id=LARGE_TELEGRAM_ID_A, level="B1")
    db_session.add(user)
    await db_session.flush()

    training_session = TrainingSession(
        user_id=user.telegram_id,
        mode="topic",
        target_topic="Nebensätze mit weil",
        total_exercises=5,
        status="active",
        created_at=FIXED_UTC_1,
    )
    db_session.add(training_session)
    await db_session.flush()

    question_payload = {
        "instruction_ua": "Переклади речення німецькою:",
        "task_text": "Я запізнився на зустріч, тому що потяг мав затримку.",
        "hint_ua": "Пам'ятай про порядок слів після weil.",
        "reference_answer": "Ich habe mich zum Treffen verspätet, weil der Zug Verspätung hatte.",
    }
    unfinished_attempt = Attempt(
        session_id=training_session.id,
        user_id=user.telegram_id,
        word_ids=[14, 29],
        exercise_type="translation",
        level="B1",
        grammar_topic="Nebensätze mit weil",
        question_payload=question_payload,
        created_at=FIXED_UTC_1,
    )
    db_session.add(unfinished_attempt)
    await db_session.commit()

    fetched_session = await db_session.get(TrainingSession, training_session.id)
    assert fetched_session is not None
    assert fetched_session.mode == "topic"
    assert fetched_session.status == "active"
    assert fetched_session.created_at == FIXED_UTC_1

    fetched_attempt = await db_session.get(Attempt, unfinished_attempt.id)
    assert fetched_attempt is not None
    assert fetched_attempt.word_ids == [14, 29]
    assert fetched_attempt.question_payload == question_payload
    assert fetched_attempt.used_hint is False
    assert fetched_attempt.user_answer is None
    assert fetched_attempt.is_correct is None
    assert fetched_attempt.grammar_score is None
    assert fetched_attempt.fsrs_ratings is None
    assert fetched_attempt.youtube_topic is None
    assert fetched_attempt.created_at == FIXED_UTC_1

    # Complete evaluation update
    ratings_payload = [
        {"word_id": 14, "fsrs_rating": 2},
        {"word_id": 29, "fsrs_rating": 2},
    ]
    fetched_attempt.user_answer = "Ich kam zu spät, weil der Zug Verspätung hatte."
    fetched_attempt.used_hint = True
    fetched_attempt.is_correct = True
    fetched_attempt.grammar_score = 92
    fetched_attempt.fsrs_ratings = ratings_payload
    fetched_attempt.youtube_topic = "Порядок слів у реченні з weil німецька B1"
    fetched_session.status = "completed"
    await db_session.commit()

    updated_attempt = await db_session.get(Attempt, unfinished_attempt.id)
    assert updated_attempt is not None
    assert updated_attempt.used_hint is True
    assert updated_attempt.is_correct is True
    assert updated_attempt.grammar_score == 92
    assert updated_attempt.fsrs_ratings == ratings_payload
    assert updated_attempt.youtube_topic == "Порядок слів у реченні з weil німецька B1"

    session_id = training_session.id
    attempt_id = unfinished_attempt.id

    await db_session.delete(fetched_session)
    await db_session.commit()
    db_session.expire_all()
    assert await db_session.get(TrainingSession, session_id) is None
    assert await db_session.get(Attempt, attempt_id) is None


async def test_weekly_snapshots_crud(db_session: AsyncSession) -> None:
    user = User(telegram_id=LARGE_TELEGRAM_ID_A)
    db_session.add(user)
    await db_session.flush()

    snapshot = WeeklySnapshot(
        user_id=user.telegram_id,
        week_start=FIXED_WEEK_START,
        vocab_score=42,
        grammar_score=68,
        consistency_score=85,
        writing_score=0,
        listening_score=0,
    )
    db_session.add(snapshot)
    await db_session.commit()

    fetched = await db_session.get(WeeklySnapshot, snapshot.id)
    assert fetched is not None
    assert fetched.week_start == FIXED_WEEK_START
    assert fetched.vocab_score == 42
    assert fetched.grammar_score == 68
    assert fetched.consistency_score == 85
    assert fetched.writing_score == 0
    assert fetched.listening_score == 0

    fetched.vocab_score = 55
    await db_session.commit()
    updated = await db_session.get(WeeklySnapshot, snapshot.id)
    assert updated is not None
    assert updated.vocab_score == 55

    await db_session.delete(updated)
    await db_session.commit()
    assert await db_session.get(WeeklySnapshot, snapshot.id) is None


async def test_words_unique_case_trim_within_deck_and_cross_user_isolation(
    db_session: AsyncSession,
) -> None:
    user_a = User(telegram_id=LARGE_TELEGRAM_ID_A)
    user_b = User(telegram_id=LARGE_TELEGRAM_ID_B)
    db_session.add_all([user_a, user_b])
    await db_session.flush()

    deck_a = Deck(owner_id=user_a.telegram_id, title="User A Deck", level="A1")
    deck_b = Deck(owner_id=user_b.telegram_id, title="User B Deck", level="A1")
    db_session.add_all([deck_a, deck_b])
    await db_session.flush()
    deck_a_id = deck_a.id
    deck_b_id = deck_b.id

    db_session.add(
        Word(
            deck_id=deck_a_id,
            word_de="  Der Tisch ",
            translation_ua="стіл",
            source="text",
        ),
    )
    await db_session.commit()

    # Duplicate in the SAME deck differing only by case/whitespace must fail
    db_session.add(
        Word(
            deck_id=deck_a_id,
            word_de="der tisch",
            translation_ua="стіл (дубль)",
            source="text",
        ),
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # Same word in ANOTHER user's deck must succeed cleanly
    other_user_word = Word(
        deck_id=deck_b_id,
        word_de="der tisch",
        translation_ua="стіл",
        source="text",
    )
    db_session.add(other_user_word)
    await db_session.commit()

    saved = await db_session.execute(select(Word).where(Word.deck_id == deck_b_id))
    assert saved.scalar_one().word_de == "der tisch"


async def test_weekly_snapshots_unique_per_user_week_and_cross_user_isolation(
    db_session: AsyncSession,
) -> None:
    user_a = User(telegram_id=LARGE_TELEGRAM_ID_A)
    user_b = User(telegram_id=LARGE_TELEGRAM_ID_B)
    db_session.add_all([user_a, user_b])
    await db_session.flush()

    db_session.add(
        WeeklySnapshot(
            user_id=user_a.telegram_id,
            week_start=FIXED_WEEK_START,
            vocab_score=30,
            grammar_score=40,
            consistency_score=50,
        ),
    )
    await db_session.commit()

    # Duplicate (user_a, week_start) must fail
    db_session.add(
        WeeklySnapshot(
            user_id=user_a.telegram_id,
            week_start=FIXED_WEEK_START,
            vocab_score=35,
            grammar_score=45,
            consistency_score=55,
        ),
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # Same week_start for user_b must succeed
    user_b_snapshot = WeeklySnapshot(
        user_id=LARGE_TELEGRAM_ID_B,
        week_start=FIXED_WEEK_START,
        vocab_score=20,
        grammar_score=25,
        consistency_score=90,
    )
    db_session.add(user_b_snapshot)
    await db_session.commit()

    fetched = await db_session.get(WeeklySnapshot, user_b_snapshot.id)
    assert fetched is not None
    assert fetched.user_id == LARGE_TELEGRAM_ID_B
    assert fetched.week_start == FIXED_WEEK_START


async def test_check_constraints_reject_invalid_values(db_session: AsyncSession) -> None:
    # Invalid user values
    for invalid_user in (
        User(telegram_id=1001, level="C1"),
        User(telegram_id=1002, theme="neon"),
        User(telegram_id=1003, reminder_hour=-1),
        User(telegram_id=1004, reminder_hour=24),
        User(telegram_id=1005, exercises_per_set=0),
        User(telegram_id=1006, exercises_per_set=31),
        User(telegram_id=1007, daily_llm_calls=-1),
    ):
        db_session.add(invalid_user)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    # Create valid parent entities for child CHECK constraint tests
    user = User(telegram_id=LARGE_TELEGRAM_ID_A)
    db_session.add(user)
    await db_session.commit()

    # Invalid deck level
    db_session.add(Deck(owner_id=LARGE_TELEGRAM_ID_A, title="Bad", level="C2"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    deck = Deck(owner_id=LARGE_TELEGRAM_ID_A, title="Valid", level="A1")
    db_session.add(deck)
    await db_session.commit()
    deck_id = deck.id

    # Invalid word source
    db_session.add(
        Word(deck_id=deck_id, word_de="das Haus", translation_ua="дім", source="clipboard"),
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    # Invalid session mode / status / total_exercises
    for invalid_session in (
        TrainingSession(user_id=LARGE_TELEGRAM_ID_A, mode="random"),
        TrainingSession(user_id=LARGE_TELEGRAM_ID_A, status="paused"),
        TrainingSession(user_id=LARGE_TELEGRAM_ID_A, total_exercises=0),
    ):
        db_session.add(invalid_session)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    valid_session = TrainingSession(user_id=LARGE_TELEGRAM_ID_A)
    db_session.add(valid_session)
    await db_session.commit()
    session_id = valid_session.id

    # Invalid attempt exercise_type / level / grammar_score
    for invalid_attempt in (
        Attempt(
            session_id=session_id,
            user_id=LARGE_TELEGRAM_ID_A,
            word_ids=[1],
            exercise_type="speaking",
            level="A1",
            grammar_topic="Artikel",
            question_payload={"task_text": "test"},
        ),
        Attempt(
            session_id=session_id,
            user_id=LARGE_TELEGRAM_ID_A,
            word_ids=[1],
            exercise_type="translation",
            level="C1",
            grammar_topic="Artikel",
            question_payload={"task_text": "test"},
        ),
        Attempt(
            session_id=session_id,
            user_id=LARGE_TELEGRAM_ID_A,
            word_ids=[1],
            exercise_type="translation",
            level="A1",
            grammar_topic="Artikel",
            question_payload={"task_text": "test"},
            grammar_score=-1,
        ),
        Attempt(
            session_id=session_id,
            user_id=LARGE_TELEGRAM_ID_A,
            word_ids=[1],
            exercise_type="translation",
            level="A1",
            grammar_topic="Artikel",
            question_payload={"task_text": "test"},
            grammar_score=101,
        ),
    ):
        db_session.add(invalid_attempt)
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    # Invalid weekly_snapshots score out of 0..100
    db_session.add(
        WeeklySnapshot(
            user_id=LARGE_TELEGRAM_ID_A,
            week_start=FIXED_WEEK_START,
            vocab_score=101,
        ),
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_deleting_user_cascades_personal_data_and_preserves_shared_deck(
    db_session: AsyncSession,
) -> None:
    user = User(telegram_id=LARGE_TELEGRAM_ID_A)
    db_session.add(user)
    await db_session.flush()

    personal_deck = Deck(owner_id=user.telegram_id, title="Personal", level="A1")
    shared_deck = Deck(owner_id=None, title="Shared A1", level="A1")
    db_session.add_all([personal_deck, shared_deck])
    await db_session.flush()

    word = Word(deck_id=shared_deck.id, word_de="das Buch", translation_ua="книга", source="system")
    db_session.add(word)
    await db_session.flush()

    card = UserWordFSRS(user_id=user.telegram_id, word_id=word.id, due_at=FIXED_UTC_1)
    snapshot = WeeklySnapshot(user_id=user.telegram_id, week_start=FIXED_WEEK_START)
    db_session.add_all([card, snapshot])
    await db_session.commit()

    personal_deck_id = personal_deck.id
    shared_deck_id = shared_deck.id
    word_id = word.id
    snapshot_id = snapshot.id

    await db_session.delete(user)
    await db_session.commit()
    db_session.expire_all()

    assert await db_session.get(Deck, personal_deck_id) is None
    assert await db_session.get(UserWordFSRS, (LARGE_TELEGRAM_ID_A, word_id)) is None
    assert await db_session.get(WeeklySnapshot, snapshot_id) is None
    # Shared deck and its system words must remain intact
    assert await db_session.get(Deck, shared_deck_id) is not None
    assert await db_session.get(Word, word_id) is not None
