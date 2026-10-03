from functools import lru_cache

from pydantic import field_validator
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

    # Ingestion
    storage_dir: str = "./data/uploads"  # uploaded files; a shared volume in Docker
    max_upload_bytes: int = 10 * 1024 * 1024
    url_fetch_max_bytes: int = 5 * 1024 * 1024
    url_fetch_timeout_seconds: float = 20.0
    ingest_job_timeout_seconds: int = 600

    # Retrieval
    rrf_k: int = 60  # Reciprocal Rank Fusion constant
    retrieval_candidates: int = 30  # depth of each ranking (vector, keyword) before fusion
    rerank_candidates: int = 15  # fused results sent to the reranker
    # Minimum top vector similarity for has_relevant_context. Empty: the embedding provider's
    # suggested value (similarities are model-specific).
    relevance_threshold: float | None = None
    query_embedding_cache_ttl_seconds: int = 24 * 3600
    # pgvector HNSW: keep scanning until enough rows pass the tenant filter (off|strict_order|
    # relaxed_order), and the candidate list size per scan step.
    hnsw_iterative_scan: str = "relaxed_order"
    hnsw_ef_search: int = 100
    strong_keyword_min_idf: float = 1.5

    # Chat
    chat_retrieval_mode: str = "hybrid"  # search tool mode; hybrid_rerank turns the reranker on
    chat_top_k: int = 8
    chat_max_searches: int = 2  # search_knowledge calls allowed per customer message
    chat_history_messages: int = 6
    chat_first_token_timeout_seconds: float = 25.0
    chat_total_timeout_seconds: float = 90.0
    # Public-endpoint protection (widget keys are embedded in websites).
    chat_rate_per_key_per_minute: int = 60
    chat_rate_per_visitor_per_minute: int = 10
    chat_daily_message_cap: int = 2000  # per tenant; tenant settings may set a lower/higher cap

    # Human handoff and channels.
    # 32 random bytes, base64 (python -c "import os,base64;print(base64.b64encode(os.urandom(32))
    # .decode())"). Encrypts stored secrets such as Telegram bot tokens; required to store one.
    secrets_encryption_key: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    # Link in staff alerts; {conversation_id} is filled in.
    admin_conversation_url: str = "http://localhost:8000/v1/conversations/{conversation_id}"
    chat_poll_per_visitor_per_minute: int = 30  # widget polling for staff replies

    # Built widget bundle (web/widget/dist), served at /widget.js. A mounted volume in Docker.
    widget_dist_dir: str = "../web/widget/dist"

    @field_validator("relevance_threshold", mode="before")
    @classmethod
    def _empty_threshold_means_default(cls, value: object) -> object:
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
