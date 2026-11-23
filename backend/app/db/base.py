from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

SUPPORTED_POSTGRES_SCHEMES = frozenset({"postgresql", "postgresql+asyncpg"})

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all ORM models with deterministic constraint naming."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_engine: AsyncEngine | None = None
_engine_url: str | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def normalize_async_database_url(database_url: str | None) -> str:
    """Validate and normalize a PostgreSQL URL to ``postgresql+asyncpg://...``.

    Accepts ``postgresql://...`` and ``postgresql+asyncpg://...`` schemes and
    never includes credentials or raw DSN strings in raised ValueErrors.
    """
    if database_url is None:
        raise ValueError("DATABASE_URL is not configured")

    raw = database_url.strip()
    if not raw or any(ch.isspace() for ch in raw):
        raise ValueError("DATABASE_URL is invalid")

    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("DATABASE_URL is invalid") from exc

    scheme = parsed.scheme.lower()
    if scheme not in SUPPORTED_POSTGRES_SCHEMES:
        raise ValueError("Unsupported DATABASE_URL scheme")

    remainder = raw[len(parsed.scheme) :]
    if not remainder.startswith("://"):
        raise ValueError("DATABASE_URL is invalid")

    if not parsed.hostname:
        raise ValueError("DATABASE_URL host is missing")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("DATABASE_URL port is invalid") from exc

    if port is not None and not (1 <= port <= 65535):
        raise ValueError("DATABASE_URL port is out of range")

    return f"postgresql+asyncpg{remainder}"


def create_async_db_engine(database_url: str, **engine_kwargs: Any) -> AsyncEngine:
    """Create a SQLAlchemy 2.0 async engine for PostgreSQL via asyncpg."""
    normalized_url = normalize_async_database_url(database_url)
    kwargs = dict(engine_kwargs)
    if "poolclass" not in kwargs:
        kwargs.setdefault("pool_pre_ping", True)
    return create_async_engine(normalized_url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async_sessionmaker configured with expire_on_commit=False."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


def get_engine() -> AsyncEngine:
    """Lazily initialize and return the cached application AsyncEngine."""
    global _engine, _engine_url, _session_factory

    normalized_url = normalize_async_database_url(get_settings().database_url)
    if _engine is None or _engine_url != normalized_url:
        _engine = create_async_db_engine(normalized_url)
        _engine_url = normalized_url
        _session_factory = None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Lazily initialize and return the cached async_sessionmaker."""
    global _session_factory

    engine = get_engine()
    if _session_factory is None:
        _session_factory = create_session_factory(engine)
    return _session_factory


async def dispose_engine() -> None:
    """Dispose the cached AsyncEngine and reset module-level DB state."""
    global _engine, _engine_url, _session_factory

    engine = _engine
    _engine = None
    _engine_url = None
    _session_factory = None
    if engine is not None:
        await engine.dispose()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an async SQLAlchemy session."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
