"""Query embedding cache, so a repeated question does not pay for a second embedding call.

Keyed by embedding model, tenant and normalized query. Per-tenant keys mean one tenant's
questions can never be observed (even through cache timing) by another. Cache failures are
logged and ignored: search still works, it just embeds again.
"""

import hashlib
import logging
import re
import unicodedata
import uuid
from array import array
from typing import Protocol

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


def normalize_query(query: str) -> str:
    """Unicode NFKC, collapsed whitespace. The cache key additionally ignores case."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", query)).strip()


def cache_key(model: str, tenant_id: uuid.UUID, query: str) -> str:
    digest = hashlib.sha256(normalize_query(query).casefold().encode()).hexdigest()
    return f"bap:qemb:v1:{model}:{tenant_id}:{digest}"


class QueryEmbeddingCache(Protocol):
    async def get(self, model: str, tenant_id: uuid.UUID, query: str) -> list[float] | None: ...

    async def set(
        self, model: str, tenant_id: uuid.UUID, query: str, embedding: list[float]
    ) -> None: ...


class RedisQueryEmbeddingCache:
    def __init__(self, redis_url: str, ttl_seconds: int) -> None:
        self._redis = Redis.from_url(redis_url, socket_timeout=1, socket_connect_timeout=1)
        self._ttl = ttl_seconds

    async def get(self, model: str, tenant_id: uuid.UUID, query: str) -> list[float] | None:
        try:
            raw = await self._redis.get(cache_key(model, tenant_id, query))
        except Exception as exc:
            logger.warning("Query embedding cache read failed: %s", type(exc).__name__)
            return None
        if raw is None:
            return None
        vector = array("f")
        vector.frombytes(raw)  # float32 is plenty for a query vector, and 4x smaller than JSON
        return vector.tolist()

    async def set(
        self, model: str, tenant_id: uuid.UUID, query: str, embedding: list[float]
    ) -> None:
        try:
            await self._redis.set(
                cache_key(model, tenant_id, query), array("f", embedding).tobytes(), ex=self._ttl
            )
        except Exception as exc:
            logger.warning("Query embedding cache write failed: %s", type(exc).__name__)

    async def aclose(self) -> None:
        await self._redis.aclose()


class InMemoryQueryEmbeddingCache:
    """For tests and single-process use."""

    def __init__(self) -> None:
        self.entries: dict[str, list[float]] = {}

    async def get(self, model: str, tenant_id: uuid.UUID, query: str) -> list[float] | None:
        return self.entries.get(cache_key(model, tenant_id, query))

    async def set(
        self, model: str, tenant_id: uuid.UUID, query: str, embedding: list[float]
    ) -> None:
        self.entries[cache_key(model, tenant_id, query)] = list(embedding)
