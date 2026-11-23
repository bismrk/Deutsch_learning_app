import configparser
import subprocess
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from app.db.base import create_async_db_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent
EXPECTED_APP_TABLES = frozenset(
    {
        "users",
        "decks",
        "words",
        "user_word_fsrs",
        "sessions",
        "attempts",
        "weekly_snapshots",
    },
)
EXPECTED_CUSTOM_INDEXES = frozenset(
    {
        "ix_user_word_fsrs_user_id_due_at",
        "ix_attempts_user_id_created_at_desc",
        "uq_words_deck_id_word_de_normalized",
        "uq_weekly_snapshots_user_id_week_start",
    },
)


def test_alembic_ini_contains_no_hardcoded_database_url() -> None:
    parser = configparser.ConfigParser()
    parser.read(BACKEND_DIR / "alembic.ini", encoding="utf-8")

    sqlalchemy_url = parser.get("alembic", "sqlalchemy.url", fallback="").strip()
    assert sqlalchemy_url == ""


async def _fetch_public_tables_and_indexes(
    database_url: str,
) -> tuple[set[str], dict[str, tuple[str, str]]]:
    engine = create_async_db_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tables_result = await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"),
            )
            tables = {row.tablename for row in tables_result}

            indexes_result = await conn.execute(
                text(
                    """
                    SELECT tablename, indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                    """,
                ),
            )
            indexes = {row.indexname: (row.tablename, row.indexdef) for row in indexes_result}
            return tables, indexes
    finally:
        await engine.dispose()


async def test_alembic_upgrade_downgrade_upgrade_cycle(
    test_database_url: str,
    alembic_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    try:
        # 1. Upgrade to head
        up_first = alembic_runner(test_database_url, "upgrade", "head")
        assert up_first.returncode == 0, f"STDERR:\n{up_first.stderr}\nSTDOUT:\n{up_first.stdout}"

        tables_after_up, indexes_after_up = await _fetch_public_tables_and_indexes(
            test_database_url,
        )
        assert EXPECTED_APP_TABLES.issubset(tables_after_up)
        assert EXPECTED_CUSTOM_INDEXES.issubset(indexes_after_up.keys())

        # 2. Downgrade to base — no application tables or indexes may remain
        down_res = alembic_runner(test_database_url, "downgrade", "base")
        assert down_res.returncode == 0, f"STDERR:\n{down_res.stderr}\nSTDOUT:\n{down_res.stdout}"

        tables_after_down, indexes_after_down = await _fetch_public_tables_and_indexes(
            test_database_url,
        )
        assert EXPECTED_APP_TABLES.isdisjoint(tables_after_down)
        remaining_app_indexes = {
            idx_name
            for idx_name, (tbl_name, _) in indexes_after_down.items()
            if tbl_name != "alembic_version"
        }
        assert remaining_app_indexes == set()

        # 3. Re-upgrade to head cleanly
        up_second = alembic_runner(test_database_url, "upgrade", "head")
        assert up_second.returncode == 0, (
            f"STDERR:\n{up_second.stderr}\nSTDOUT:\n{up_second.stdout}"
        )

        tables_after_reup, indexes_after_reup = await _fetch_public_tables_and_indexes(
            test_database_url,
        )
        assert EXPECTED_APP_TABLES.issubset(tables_after_reup)
        assert EXPECTED_CUSTOM_INDEXES.issubset(indexes_after_reup.keys())
    finally:
        # Always restore head so subsequent tests see the migrated schema
        alembic_runner(test_database_url, "upgrade", "head")


def test_alembic_accepts_plain_postgresql_url_scheme(
    migrated_test_db_url: str,
    alembic_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    plain_postgres_url = migrated_test_db_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    assert plain_postgres_url.startswith("postgresql://")

    result = alembic_runner(plain_postgres_url, "current")
    assert result.returncode == 0, f"STDERR:\n{result.stderr}\nSTDOUT:\n{result.stdout}"
    assert "(head)" in result.stdout


async def test_required_indexes_exist_in_pg_indexes(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            """
            SELECT tablename, indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
            """,
        ),
    )
    indexes = {row.indexname: (row.tablename, row.indexdef) for row in result}

    # 1. user_word_fsrs(user_id, due_at)
    assert "ix_user_word_fsrs_user_id_due_at" in indexes
    fsrs_table, fsrs_def = indexes["ix_user_word_fsrs_user_id_due_at"]
    assert fsrs_table == "user_word_fsrs"
    assert "(user_id, due_at)" in fsrs_def

    # 2. attempts(user_id, created_at DESC)
    assert "ix_attempts_user_id_created_at_desc" in indexes
    attempts_table, attempts_def = indexes["ix_attempts_user_id_created_at_desc"]
    assert attempts_table == "attempts"
    assert "(user_id, created_at DESC)" in attempts_def

    # 3. Unique functional index on words(deck_id, lower(trim(word_de)))
    assert "uq_words_deck_id_word_de_normalized" in indexes
    words_table, words_def = indexes["uq_words_deck_id_word_de_normalized"]
    assert words_table == "words"
    assert words_def.startswith("CREATE UNIQUE INDEX ")
    words_def_lower = words_def.lower()
    assert "deck_id" in words_def_lower
    assert "lower(" in words_def_lower
    assert "trim(" in words_def_lower
    assert "word_de" in words_def_lower

    # 4. Unique index/constraint on weekly_snapshots(user_id, week_start)
    assert "uq_weekly_snapshots_user_id_week_start" in indexes
    snap_table, snap_def = indexes["uq_weekly_snapshots_user_id_week_start"]
    assert snap_table == "weekly_snapshots"
    assert snap_def.startswith("CREATE UNIQUE INDEX ")
    assert "(user_id, week_start)" in snap_def
