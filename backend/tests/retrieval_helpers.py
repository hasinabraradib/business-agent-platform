"""Shared helpers for retrieval tests: crafted corpora embedded with the fake provider."""

import json
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.embeddings import EmbeddingInput, FakeEmbeddingProvider
from app.retrieval.cache import InMemoryQueryEmbeddingCache
from app.retrieval.rerank import NoopReranker, Reranker
from app.retrieval.service import RetrievalConfig, Retriever

FAKE = FakeEmbeddingProvider()


class CountingEmbedder(FakeEmbeddingProvider):
    def __init__(self) -> None:
        self.query_calls: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.query_calls.append(query)
        return await super().embed_query(query)


async def add_document(
    owner_engine: AsyncEngine,
    tenant_id: uuid.UUID,
    title: str,
    chunks: Sequence[str | tuple[str, dict]],
) -> list[uuid.UUID]:
    """Insert a ready document and its chunks (fake embeddings of title + content)."""
    items = [(c, {}) if isinstance(c, str) else c for c in chunks]
    vectors = await FAKE.embed_documents([EmbeddingInput(content, title) for content, _ in items])
    async with owner_engine.begin() as conn:
        document_id = await conn.scalar(
            text(
                "INSERT INTO documents (tenant_id, title, source_type, status, chunk_count) "
                "VALUES (:t, :title, 'text', 'ready', :n) RETURNING id"
            ),
            {"t": tenant_id, "title": title, "n": len(items)},
        )
        ids = []
        for index, ((content, metadata), vector) in enumerate(zip(items, vectors, strict=True)):
            ids.append(
                await conn.scalar(
                    text(
                        "INSERT INTO chunks (tenant_id, document_id, chunk_index, content, "
                        "metadata, embedding, embedding_model) VALUES (:t, :d, :i, :c, "
                        "CAST(:m AS jsonb), CAST(:e AS vector), :model) RETURNING id"
                    ),
                    {
                        "t": tenant_id,
                        "d": document_id,
                        "i": index,
                        "c": content,
                        "m": json.dumps(metadata),
                        "e": str(vector),
                        "model": FAKE.model_name,
                    },
                )
            )
    return ids


def make_retriever(
    app_engine: AsyncEngine,
    *,
    embedder: FakeEmbeddingProvider | None = None,
    reranker: Reranker | None = None,
    cache=None,
    **config,
) -> Retriever:
    return Retriever(
        sessionmaker=async_sessionmaker(app_engine, expire_on_commit=False),
        embedder=embedder or FakeEmbeddingProvider(),
        cache=cache if cache is not None else InMemoryQueryEmbeddingCache(),
        reranker=reranker or NoopReranker(),
        config=RetrievalConfig(**config),
    )
