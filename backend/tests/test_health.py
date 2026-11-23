from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.main import app

MANAGED_ENV_VARS = (
    "APP_NAME",
    "APP_ENV",
    "LOG_LEVEL",
    "DATABASE_URL",
    "BOT_TOKEN",
    "INTERNAL_API_KEY",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "DAILY_LLM_LIMIT",
)


def _clear_managed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var_name in MANAGED_ENV_VARS:
        monkeypatch.delenv(var_name, raising=False)


async def test_health_returns_ok_json(client: AsyncClient) -> None:
    response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok"}


async def test_health_works_on_default_app_instance() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.get("/api/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok"}


async def test_health_post_returns_method_not_allowed(client: AsyncClient) -> None:
    response = await client.post("/api/health")

    assert response.status_code == 405


async def test_unprefixed_health_returns_not_found(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 404


async def test_unknown_api_route_returns_not_found(client: AsyncClient) -> None:
    response = await client.get("/api/nonexistent-endpoint")

    assert response.status_code == 404


async def test_openapi_schema_includes_health_route(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")

    schema = response.json()
    assert "/api/health" in schema["paths"]
    assert "get" in schema["paths"]["/api/health"]


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
    monkeypatch.setenv("CADDY_DOMAIN", "example.com")
    monkeypatch.setenv("UNRELATED_SERVICE_FLAG", "true")

    settings = Settings()

    assert settings.app_env == "development"
    assert not hasattr(settings, "postgres_password")
    assert not hasattr(settings, "caddy_domain")
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
