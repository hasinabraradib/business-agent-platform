"""Embedding providers behind one interface, chosen by the EMBEDDING_PROVIDER setting.

To add a provider (e.g. OpenAI): write a subclass of EmbeddingProvider in this package, add its
settings to EmbeddingSettings, and register a factory in PROVIDERS. Nothing outside this package
changes, as long as it returns EMBEDDING_DIMENSIONS-long vectors.
"""

import logging
from collections.abc import Callable

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.embeddings.base import EmbeddingError, EmbeddingInput, EmbeddingProvider
from app.embeddings.fake import FakeEmbeddingProvider
from app.embeddings.gemini import DEFAULT_MODEL as GEMINI_DEFAULT_MODEL
from app.embeddings.gemini import GeminiEmbeddingProvider

__all__ = [
    "EmbeddingError",
    "EmbeddingInput",
    "EmbeddingProvider",
    "EmbeddingSettings",
    "FakeEmbeddingProvider",
    "GeminiEmbeddingProvider",
    "get_embedding_provider",
]

logger = logging.getLogger(__name__)


class EmbeddingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # auto: gemini when GEMINI_API_KEY is set, otherwise fake. Or name a provider explicitly.
    embedding_provider: str = "auto"
    gemini_api_key: str = ""
    gemini_embedding_model: str = GEMINI_DEFAULT_MODEL


PROVIDERS: dict[str, Callable[[EmbeddingSettings], EmbeddingProvider]] = {
    "fake": lambda settings: FakeEmbeddingProvider(),
    "gemini": lambda settings: GeminiEmbeddingProvider(
        settings.gemini_api_key, settings.gemini_embedding_model
    ),
}


def get_embedding_provider(settings: EmbeddingSettings | None = None) -> EmbeddingProvider:
    settings = settings or EmbeddingSettings()
    name = settings.embedding_provider.strip().lower()
    if name == "auto":
        name = "gemini" if settings.gemini_api_key else "fake"
        if name == "fake":
            logger.warning("No GEMINI_API_KEY set: using fake embeddings (not for production)")
    if name not in PROVIDERS:
        raise ValueError(f"Unknown EMBEDDING_PROVIDER {name!r}; choose from {sorted(PROVIDERS)}")
    return PROVIDERS[name](settings)
