"""Retrieval: find the chunks most relevant to a tenant's question. See service.Retriever."""

from functools import lru_cache

from app.config import get_settings
from app.db import get_sessionmaker
from app.embeddings import get_embedding_provider
from app.retrieval.cache import RedisQueryEmbeddingCache
from app.retrieval.rerank import RerankSettings, get_reranker
from app.retrieval.service import (
    MODES,
    RetrievalConfig,
    RetrievalMode,
    RetrievalResult,
    RetrievedChunk,
    Retriever,
)

__all__ = [
    "MODES",
    "RetrievalConfig",
    "RetrievalMode",
    "RetrievalResult",
    "RetrievedChunk",
    "Retriever",
    "get_retriever",
]


@lru_cache
def get_retriever() -> Retriever:
    """The process-wide Retriever (application-role database, configured providers)."""
    settings = get_settings()
    rerank_settings = RerankSettings()
    return Retriever(
        sessionmaker=get_sessionmaker(),
        embedder=get_embedding_provider(),
        cache=RedisQueryEmbeddingCache(
            settings.redis_url, settings.query_embedding_cache_ttl_seconds
        ),
        reranker=get_reranker(rerank_settings),
        config=RetrievalConfig(
            rrf_k=settings.rrf_k,
            candidates=settings.retrieval_candidates,
            rerank_candidates=settings.rerank_candidates,
            rerank_timeout_seconds=rerank_settings.rerank_timeout_seconds,
            relevance_threshold=settings.relevance_threshold,
            iterative_scan=settings.hnsw_iterative_scan,
            ef_search=settings.hnsw_ef_search,
            strong_keyword_min_idf=settings.strong_keyword_min_idf,
        ),
    )
