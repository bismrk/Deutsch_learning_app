import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Annotated, Literal
from urllib.parse import urlsplit

import asyncpg
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

SUPPORTED_POSTGRES_SCHEMES = frozenset({"postgresql", "postgresql+asyncpg"})
DEFAULT_DB_HEALTHCHECK_TIMEOUT = 2.0
CLOSE_TIMEOUT_SECONDS = 0.5

DbHealthChecker = Callable[[], Awaitable[bool]]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    database: Literal["ok"] = "ok"


class HealthErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    database: Literal["unavailable"] = "unavailable"


def normalize_asyncpg_dsn(database_url: str | None) -> str:
    """Validate and normalize a PostgreSQL DATABASE_URL for asyncpg.connect().

    Supports both ``postgresql://...`` and ``postgresql+asyncpg://...`` schemes
    and normalizes them to ``postgresql://...`` without leaking secrets in errors.
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

    return f"postgresql{remainder}"


async def check_postgres_health(
    database_url: str | None,
    *,
    timeout: float = DEFAULT_DB_HEALTHCHECK_TIMEOUT,
) -> bool:
    """Verify PostgreSQL connectivity by executing SELECT 1 within timeout."""
    effective_timeout = min(timeout, 3.0)
    if effective_timeout <= 0:
        return False

    try:
        dsn = normalize_asyncpg_dsn(database_url)
    except ValueError:
        logger.warning("Database health check skipped: DATABASE_URL is missing or invalid")
        return False

    conn: asyncpg.Connection | None = None
    try:
        async with asyncio.timeout(effective_timeout):
            conn = await asyncpg.connect(dsn=dsn, timeout=effective_timeout)
            result = await conn.fetchval("SELECT 1", timeout=effective_timeout)
            return result == 1
    except Exception as exc:
        logger.warning("Database health check failed (%s)", type(exc).__name__)
        return False
    finally:
        if conn is not None:
            try:
                async with asyncio.timeout(min(effective_timeout, CLOSE_TIMEOUT_SECONDS)):
                    await conn.close()
            except Exception:
                terminate = getattr(conn, "terminate", None)
                if callable(terminate):
                    with suppress(Exception):
                        terminate()


def get_app_settings(request: Request) -> Settings:
    """Return the Settings instance attached to the active FastAPI application."""
    app_settings = getattr(request.app.state, "settings", None)
    if isinstance(app_settings, Settings):
        return app_settings
    return get_settings()


def get_db_health_checker(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> DbHealthChecker:
    """Provide the async PostgreSQL health check callable as a FastAPI dependency."""

    async def _check() -> bool:
        return await check_postgres_health(
            settings.database_url,
            timeout=settings.db_healthcheck_timeout,
        )

    return _check


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthErrorResponse}},
)
async def health_check(
    check_db: Annotated[DbHealthChecker, Depends(get_db_health_checker)],
) -> HealthResponse | JSONResponse:
    """Return service and PostgreSQL connectivity status."""
    try:
        is_healthy = await check_db()
    except Exception as exc:
        logger.warning("Database health check dependency failed (%s)", type(exc).__name__)
        is_healthy = False

    if not is_healthy:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=HealthErrorResponse().model_dump(),
        )

    return HealthResponse()
