import asyncio
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.health import (
    check_postgres_health,
    get_db_health_checker,
    normalize_asyncpg_dsn,
)
from app.config import Settings, get_settings
from app.main import app, create_app

MANAGED_ENV_VARS = (
    "APP_NAME",
    "APP_ENV",
    "LOG_LEVEL",
    "DATABASE_URL",
    "DB_HEALTHCHECK_TIMEOUT",
    "BOT_TOKEN",
    "INTERNAL_API_KEY",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "DAILY_LLM_LIMIT",
)


class FakeAsyncpgConnection:
    """In-memory asyncpg connection stub for offline unit tests."""

    def __init__(
        self,
        *,
        fetchval_result: Any = 1,
        fetchval_error: Exception | None = None,
        fetchval_hang: bool = False,
        close_error: Exception | None = None,
    ) -> None:
        self.fetchval_result = fetchval_result
        self.fetchval_error = fetchval_error
        self.fetchval_hang = fetchval_hang
        self.close_error = close_error
        self.queries: list[tuple[str, float | None]] = []
        self.closed = False
        self.terminated = False

    async def fetchval(self, query: str, *, timeout: float | None = None) -> Any:
        self.queries.append((query, timeout))
        if self.fetchval_hang:
            await asyncio.Event().wait()
        if self.fetchval_error is not None:
            raise self.fetchval_error
        return self.fetchval_result

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    def terminate(self) -> None:
        self.terminated = True


def _clear_managed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var_name in MANAGED_ENV_VARS:
        monkeypatch.delenv(var_name, raising=False)


async def test_health_returns_ok_when_db_dependency_is_healthy() -> None:
    test_app = create_app(settings=Settings(_env_file=None, app_env="test"))

    async def healthy_db() -> bool:
        return True

    test_app.dependency_overrides[get_db_health_checker] = lambda: healthy_db

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get("/api/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_health_works_on_default_app_instance_with_dependency_override() -> None:
    async def healthy_db() -> bool:
        return True

    app.dependency_overrides[get_db_health_checker] = lambda: healthy_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
            response = await async_client.get("/api/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_health_returns_503_when_db_dependency_returns_false() -> None:
    test_app = create_app(settings=Settings(_env_file=None, app_env="test"))

    async def unavailable_db() -> bool:
        return False

    test_app.dependency_overrides[get_db_health_checker] = lambda: unavailable_db

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get("/api/health")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "error", "database": "unavailable"}


async def test_health_returns_503_without_leaking_secrets_when_dependency_raises() -> None:
    secret_password = "Sup3rS3cr3tPassword!"
    secret_dsn = f"postgresql://postgres:{secret_password}@postgres:5432/deutsch_learning"
    test_app = create_app(
        settings=Settings(_env_file=None, app_env="test", database_url=secret_dsn),
    )

    async def broken_db() -> bool:
        raise RuntimeError(f"Failed connecting to {secret_dsn}")

    test_app.dependency_overrides[get_db_health_checker] = lambda: broken_db

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}
    assert secret_password not in response.text
    assert secret_dsn not in response.text
    assert "Traceback" not in response.text


@pytest.mark.parametrize(
    ("raw_dsn", "expected_dsn"),
    [
        (
            "postgresql://postgres:secret@postgres:5432/deutsch_learning",
            "postgresql://postgres:secret@postgres:5432/deutsch_learning",
        ),
        (
            "postgresql+asyncpg://postgres:secret@postgres:5432/deutsch_learning",
            "postgresql://postgres:secret@postgres:5432/deutsch_learning",
        ),
    ],
)
async def test_health_executes_select_1_with_normalized_dsn(
    monkeypatch: pytest.MonkeyPatch,
    raw_dsn: str,
    expected_dsn: str,
) -> None:
    fake_conn = FakeAsyncpgConnection(fetchval_result=1)
    connect_calls: list[dict[str, Any]] = []

    async def fake_connect(*, dsn: str, timeout: float) -> FakeAsyncpgConnection:
        connect_calls.append({"dsn": dsn, "timeout": timeout})
        return fake_conn

    monkeypatch.setattr(asyncpg, "connect", fake_connect)

    test_app = create_app(
        settings=Settings(_env_file=None, app_env="test", database_url=raw_dsn),
    )
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
    assert len(connect_calls) == 1
    assert connect_calls[0]["dsn"] == expected_dsn
    assert 0 < connect_calls[0]["timeout"] <= 3.0
    assert fake_conn.queries == [("SELECT 1", connect_calls[0]["timeout"])]
    assert fake_conn.closed is True


@pytest.mark.parametrize(
    "bad_database_url",
    [
        None,
        "",
        "   ",
        "sqlite:///local.db",
        "mysql://user:secret@localhost:3306/db",
        "postgresql+psycopg://user:secret@localhost:5432/db",
        "not-a-valid-dsn",
        "postgresql://",
        "postgresql://user:secret@host:notaport/db",
    ],
)
async def test_health_returns_503_for_missing_or_invalid_database_url_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
    bad_database_url: str | None,
) -> None:
    connect_called = False

    async def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        nonlocal connect_called
        connect_called = True
        raise AssertionError("asyncpg.connect must not be called for missing/invalid DATABASE_URL")

    monkeypatch.setattr(asyncpg, "connect", fail_if_called)

    test_app = create_app(
        settings=Settings(_env_file=None, app_env="test", database_url=bad_database_url),
    )
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}
    assert connect_called is False
    assert "secret" not in response.text
    assert "Traceback" not in response.text


async def test_health_returns_503_on_connection_error_without_secret_leak(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_password = "MyVerySecretDbPassword42"
    dsn = f"postgresql+asyncpg://postgres:{secret_password}@postgres:5432/deutsch_learning"

    async def failing_connect(*, dsn: str, timeout: float) -> Any:
        raise OSError(f"Connection refused to {dsn}")

    monkeypatch.setattr(asyncpg, "connect", failing_connect)

    test_app = create_app(
        settings=Settings(_env_file=None, app_env="test", database_url=dsn),
    )
    transport = ASGITransport(app=test_app)
    with caplog.at_level("WARNING"):
        async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
            response = await async_client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}
    assert secret_password not in response.text
    assert "postgresql://" not in response.text
    assert "Traceback" not in response.text
    assert secret_password not in caplog.text
    assert "postgresql://" not in caplog.text


async def test_check_postgres_health_clamps_timeout_to_three_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_conn = FakeAsyncpgConnection(fetchval_result=1)
    captured_timeouts: list[float] = []

    async def fake_connect(*, dsn: str, timeout: float) -> FakeAsyncpgConnection:
        captured_timeouts.append(timeout)
        return fake_conn

    monkeypatch.setattr(asyncpg, "connect", fake_connect)

    is_healthy = await check_postgres_health(
        "postgresql://postgres:secret@postgres:5432/deutsch_learning",
        timeout=10.0,
    )

    assert is_healthy is True
    assert captured_timeouts == [3.0]
    assert fake_conn.queries == [("SELECT 1", 3.0)]
    assert fake_conn.closed is True


async def test_health_returns_503_on_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hanging_connect(*, dsn: str, timeout: float) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncpg, "connect", hanging_connect)

    test_app = create_app(
        settings=Settings(
            _env_file=None,
            app_env="test",
            database_url="postgresql://postgres:secret@postgres:5432/deutsch_learning",
            db_healthcheck_timeout=0.05,
        ),
    )
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}


async def test_health_returns_503_on_query_timeout_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_conn = FakeAsyncpgConnection(fetchval_hang=True)

    async def fake_connect(*, dsn: str, timeout: float) -> FakeAsyncpgConnection:
        return fake_conn

    monkeypatch.setattr(asyncpg, "connect", fake_connect)

    test_app = create_app(
        settings=Settings(
            _env_file=None,
            app_env="test",
            database_url="postgresql://postgres:secret@postgres:5432/deutsch_learning",
            db_healthcheck_timeout=0.05,
        ),
    )
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unavailable"}
    assert fake_conn.closed is True


async def test_check_postgres_health_terminates_connection_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_conn = FakeAsyncpgConnection(
        fetchval_error=RuntimeError("query failed"),
        close_error=OSError("socket already broken"),
    )

    async def fake_connect(*, dsn: str, timeout: float) -> FakeAsyncpgConnection:
        return fake_conn

    monkeypatch.setattr(asyncpg, "connect", fake_connect)

    is_healthy = await check_postgres_health(
        "postgresql+asyncpg://postgres:secret@postgres:5432/deutsch_learning",
        timeout=0.5,
    )

    assert is_healthy is False
    assert fake_conn.closed is True
    assert fake_conn.terminated is True


async def test_check_postgres_health_returns_false_for_unexpected_query_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_conn = FakeAsyncpgConnection(fetchval_result=0)

    async def fake_connect(*, dsn: str, timeout: float) -> FakeAsyncpgConnection:
        return fake_conn

    monkeypatch.setattr(asyncpg, "connect", fake_connect)

    is_healthy = await check_postgres_health(
        "postgresql://postgres:secret@postgres:5432/deutsch_learning",
        timeout=0.5,
    )

    assert is_healthy is False
    assert fake_conn.closed is True


@pytest.mark.parametrize(
    ("raw_dsn", "expected_dsn"),
    [
        (
            "postgresql://postgres:pass@localhost:5432/deutsch_learning",
            "postgresql://postgres:pass@localhost:5432/deutsch_learning",
        ),
        (
            "postgresql+asyncpg://postgres:pass@localhost:5432/deutsch_learning",
            "postgresql://postgres:pass@localhost:5432/deutsch_learning",
        ),
        (
            "  POSTGRESQL+ASYNCPG://user:p%40ss%3A1@postgres:5432/app?sslmode=require  ",
            "postgresql://user:p%40ss%3A1@postgres:5432/app?sslmode=require",
        ),
        (
            "postgresql+asyncpg://user:pass@[::1]:5432/deutsch_learning",
            "postgresql://user:pass@[::1]:5432/deutsch_learning",
        ),
        (
            "postgresql://postgres@postgres/deutsch_learning",
            "postgresql://postgres@postgres/deutsch_learning",
        ),
    ],
)
def test_normalize_asyncpg_dsn_accepts_supported_formats(
    raw_dsn: str,
    expected_dsn: str,
) -> None:
    assert normalize_asyncpg_dsn(raw_dsn) == expected_dsn


@pytest.mark.parametrize(
    "invalid_dsn",
    [
        None,
        "",
        "   ",
        "mysql://user:secret_pwd@localhost:3306/db",
        "sqlite:///test.db",
        "http://example.com/db",
        "postgresql+psycopg://user:secret_pwd@localhost:5432/db",
        "postgresql:secret_pwd@localhost/db",
        "postgresql://",
        "postgresql://:5432/db",
        "postgresql://user:secret_pwd@localhost:notaport/db",
        "postgresql://user:secret_pwd@localhost:0/db",
        "postgresql://user:secret_pwd@localhost:70000/db",
        "postgresql://user:secret_pwd@localhost:5432/db with spaces",
        "postgresql://user:secret_pwd@[broken-ipv6/db",
    ],
)
def test_normalize_asyncpg_dsn_rejects_invalid_values_without_leaking_secrets(
    invalid_dsn: str | None,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        normalize_asyncpg_dsn(invalid_dsn)

    assert "secret_pwd" not in str(exc_info.value)


async def test_health_post_returns_method_not_allowed(client: AsyncClient) -> None:
    response = await client.post("/api/health")

    assert response.status_code == 405


async def test_unprefixed_health_returns_not_found(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 404


async def test_unknown_api_route_returns_not_found(client: AsyncClient) -> None:
    response = await client.get("/api/nonexistent-endpoint")

    assert response.status_code == 404


async def test_openapi_schema_includes_health_route_and_responses(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")

    schema = response.json()
    assert "/api/health" in schema["paths"]
    health_get = schema["paths"]["/api/health"]["get"]
    assert "200" in health_get["responses"]
    assert "503" in health_get["responses"]


def test_settings_defaults_without_env_file_or_env_vars(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_managed_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.app_name == "Deutsch Learning API"
    assert settings.app_env == "development"
    assert settings.log_level == "INFO"
    assert settings.database_url is None
    assert settings.db_healthcheck_timeout == 2.0
    assert settings.bot_token is None
    assert settings.internal_api_key is None
    assert settings.llm_base_url == "https://api.deepseek.com"
    assert settings.llm_api_key is None
    assert settings.llm_model == "deepseek-chat"
    assert settings.daily_llm_limit == 50


def test_settings_applies_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_managed_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "PRODUCTION")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("DB_HEALTHCHECK_TIMEOUT", "1.5")
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN")
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    monkeypatch.setenv("LLM_MODEL", "custom-chat-model")
    monkeypatch.setenv("DAILY_LLM_LIMIT", "120")

    settings = Settings()

    assert settings.app_env == "production"
    assert settings.log_level == "DEBUG"
    assert settings.database_url == "postgresql+asyncpg://user:pass@localhost:5432/testdb"
    assert settings.db_healthcheck_timeout == 1.5
    assert settings.bot_token == "123456:TEST_TOKEN"
    assert settings.internal_api_key == "test-internal-key"
    assert settings.llm_base_url == "https://api.example.com/v1"
    assert settings.llm_api_key == "test-llm-key"
    assert settings.llm_model == "custom-chat-model"
    assert settings.daily_llm_limit == 120


def test_settings_ignores_unknown_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_managed_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POSTGRES_PASSWORD", "ignored-secret")
    monkeypatch.setenv("CADDY_SITE_ADDRESS", ":80")
    monkeypatch.setenv("UNRELATED_SERVICE_FLAG", "true")

    settings = Settings()

    assert settings.app_env == "development"
    assert not hasattr(settings, "postgres_password")
    assert not hasattr(settings, "caddy_site_address")
    assert not hasattr(settings, "unrelated_service_flag")


@pytest.mark.parametrize("invalid_env", ["invalid", "prod", "", "   "])
def test_settings_rejects_invalid_app_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_env: str,
) -> None:
    _clear_managed_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", invalid_env)

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("invalid_level", ["INVALID", "VERBOSE", "TRACE", "", "   "])
def test_settings_rejects_invalid_log_level(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_level: str,
) -> None:
    _clear_managed_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_LEVEL", invalid_level)

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("invalid_limit", ["0", "-5", "not-an-int"])
def test_settings_rejects_invalid_daily_llm_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_limit: str,
) -> None:
    _clear_managed_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DAILY_LLM_LIMIT", invalid_limit)

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("invalid_timeout", ["0", "-1", "3.1", "not-a-float"])
def test_settings_rejects_invalid_db_healthcheck_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_timeout: str,
) -> None:
    _clear_managed_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_HEALTHCHECK_TIMEOUT", invalid_timeout)

    with pytest.raises(ValidationError):
        Settings()


def test_settings_loads_dotenv_file_and_ignores_extra_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_managed_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "APP_ENV=staging\nLOG_LEVEL=WARNING\nOTHER_SERVICE_KEY=ignored\n",
        encoding="utf-8",
    )

    settings = Settings()

    assert settings.app_env == "staging"
    assert settings.log_level == "WARNING"
    assert not hasattr(settings, "other_service_key")


def test_get_settings_returns_cached_instance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_managed_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()

    try:
        first = get_settings()
        second = get_settings()
        assert first is second
    finally:
        get_settings.cache_clear()
