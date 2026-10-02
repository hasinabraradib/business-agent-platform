from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from environment variables (and an optional .env file)."""

    # The repo-root .env is shared with docker compose; a backend/.env, if present, overrides it.
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "development"
    log_level: str = "INFO"

    # The API connects as the non-superuser application role, so Row-Level Security applies.
    database_url: str = "postgresql+asyncpg://bap_app:bap_app@localhost:5432/app"
    # Migrations and the platform CLI connect as the owner role (bypasses RLS).
    owner_database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/app"
    # Tests use the same servers and roles, but this database name instead.
    test_database_name: str = "app_test"
    db_connect_timeout_seconds: float = 5.0

    redis_url: str = "redis://localhost:6379/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
