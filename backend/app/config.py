from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["development", "staging", "production", "test"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="Deutsch Learning API", min_length=1)
    app_env: AppEnv = "development"
    log_level: LogLevel = "INFO"

    database_url: str | None = None
    test_database_url: str | None = None
    db_healthcheck_timeout: float = Field(default=2.0, gt=0.0, le=3.0)
    bot_token: str | None = None
    internal_api_key: str | None = None
    init_data_max_age_seconds: int = Field(default=86400, ge=1)

    llm_base_url: str = Field(default="https://api.deepseek.com", min_length=1)
    llm_api_key: str | None = None
    llm_model: str = Field(default="deepseek-chat", min_length=1)
    daily_llm_limit: int = Field(default=50, ge=1)

    @field_validator("app_env", mode="before")
    @classmethod
    def normalize_app_env(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
