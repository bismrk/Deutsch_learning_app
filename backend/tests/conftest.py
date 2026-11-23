import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.db.base import create_async_db_engine, normalize_async_database_url
from app.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _postgres_target_key(url: str) -> tuple[str, int, str]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    port = parsed.port or 5432
    database = parsed.path.lstrip("/")
    return (host, port, database)


def _resolve_isolated_test_database_url() -> str | None:
    """Resolve and validate TEST_DATABASE_URL, refusing main DB or non-PostgreSQL URLs."""
    env_value = os.environ.get("TEST_DATABASE_URL")
    raw_test_url = env_value if env_value is not None else Settings().test_database_url
    if raw_test_url is None:
        return None

    try:
        normalized_test_url = normalize_async_database_url(raw_test_url)
    except ValueError as exc:
        pytest.fail(f"Invalid TEST_DATABASE_URL configuration: {exc}")

    test_target = _postgres_target_key(normalized_test_url)
    if not test_target[2]:
        pytest.fail("TEST_DATABASE_URL must specify a database name")

    env_main = os.environ.get("DATABASE_URL")
    raw_main_url = env_main if env_main is not None else Settings().database_url
    if raw_main_url:
        try:
            normalized_main_url = normalize_async_database_url(raw_main_url)
        except ValueError:
            normalized_main_url = None
        if normalized_main_url and _postgres_target_key(normalized_main_url) == test_target:
            pytest.fail(
                "Refusing to run tests: TEST_DATABASE_URL points to the same database as "
                "DATABASE_URL. Configure an isolated PostgreSQL test database.",
            )

    return normalized_test_url


def _run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Provide an ASGI AsyncClient backed by an isolated test FastAPI app."""
    test_app = create_app(settings=Settings(_env_file=None, app_env="test"))
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Return the validated isolated PostgreSQL TEST_DATABASE_URL or skip if unset."""
    resolved = _resolve_isolated_test_database_url()
    if resolved is None:
        pytest.skip("TEST_DATABASE_URL is not configured; skipping PostgreSQL integration tests")
    return resolved


@pytest.fixture(scope="session")
def alembic_runner() -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a helper that runs Alembic CLI commands against a specified database URL."""
    return _run_alembic


@pytest.fixture(scope="session")
def migrated_test_db_url(
    test_database_url: str,
    alembic_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    """Ensure the isolated PostgreSQL test database is migrated to Alembic head."""
    result = alembic_runner(test_database_url, "upgrade", "head")
    if result.returncode != 0:
        pytest.fail(
            f"alembic upgrade head failed (exit {result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
    return test_database_url


@pytest.fixture
async def db_session(migrated_test_db_url: str) -> AsyncIterator[AsyncSession]:
    """Provide an async SQLAlchemy session wrapped in a rolled-back outer transaction."""
    engine = create_async_db_engine(migrated_test_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session_factory = async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
                join_transaction_mode="create_savepoint",
            )
            async with session_factory() as session:
                try:
                    yield session
                finally:
                    await session.close()
            await transaction.rollback()
    finally:
        await engine.dispose()
