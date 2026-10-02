"""retrieve(tenant_id, query, mode, top_k): the one entry point for finding relevant chunks.

Modes (all kept, so they can be compared on an eval set):
- vector:        cosine similarity between the query embedding and chunk embeddings
- keyword:       idf-weighted term matching over chunks.tsv ('simple' configuration)
- hybrid:        Reciprocal Rank Fusion of the vector and keyword rankings
- hybrid_rerank: hybrid, then the top candidates reordered by a Reranker

Every response carries a confidence signal (the best vector similarity and whether it clears the
relevance threshold) so the chat step can say "I don't know" instead of guessing.
"""

import asyncio
import logging
import math
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.embeddings import EmbeddingProvider
from app.models import EMBEDDING_DIMENSIONS
from app.retrieval.cache import QueryEmbeddingCache, normalize_query
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.rerank import RerankCandidate, Reranker
from app.retrieval.search_sql import KeywordHit, VectorHit, keyword_search, vector_search
from app.tenancy import tenant_db

logger = logging.getLogger(__name__)

RetrievalMode = Literal["vector", "keyword", "hybrid", "hybrid_rerank"]
MODES: tuple[str, ...] = get_args(RetrievalMode)


@dataclass
class RetrievalConfig:
    rrf_k: int = 60
    candidates: int = 30
    rerank_candidates: int = 15
    rerank_timeout_seconds: float = 8.0
    relevance_threshold: float | None = None  # None: the embedding provider's suggestion
    iterative_scan: str = "relaxed_order"
    ef_search: int = 100
    exact_fallback: bool = True


@dataclass
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    content: str
    metadata: dict[str, Any]
    vector_score: float | None = None  # cosine similarity to the query
    keyword_score: float | None = None  # idf-weighted term score (None: no term matched)
    fused_score: float | None = None  # Reciprocal Rank Fusion score (hybrid modes)
    rerank_score: float | None = None  # reranker relevance in [0, 1] (hybrid_rerank)


@dataclass
class RetrievalResult:
    query: str
    mode: str
    chunks: list[RetrievedChunk]
    top_vector_similarity: float | None
    has_relevant_context: bool
    relevance_threshold: float
    embedding_model: str
    embedding_cached: bool
    reranker: str | None = None
    rerank_applied: bool = False
    rerank_error: str | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)


HYDRATE_SQL = text(
    """
    SELECT c.id, c.document_id, d.title, c.content, c.metadata,
           CASE WHEN c.embedding_model = :model
                THEN 1 - (c.embedding <=> :embedding) END AS similarity
    FROM chunks AS c
    JOIN documents AS d ON d.tenant_id = c.tenant_id AND d.id = c.document_id
    WHERE c.tenant_id = :tenant_id AND c.id = ANY(:ids)
    """
).bindparams(
    bindparam("embedding", type_=Vector(EMBEDDING_DIMENSIONS)),
    bindparam("ids", type_=ARRAY(UUID(as_uuid=True))),
)


class Retriever:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        embedder: EmbeddingProvider,
        cache: QueryEmbeddingCache | None,
        reranker: Reranker,
        config: RetrievalConfig | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.embedder = embedder
        self.cache = cache
        self.reranker = reranker
        self.config = config or RetrievalConfig()

    @property
    def relevance_threshold(self) -> float:
        if self.config.relevance_threshold is not None:
            return self.config.relevance_threshold
        return self.embedder.relevance_threshold

    async def retrieve(
        self, tenant_id: uuid.UUID, query: str, mode: str = "hybrid", top_k: int = 5
    ) -> RetrievalResult:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        query = normalize_query(query)
        if not query:
            raise ValueError("query must not be empty")
        timings: dict[str, float] = {}
        started = time.perf_counter()

        # Vector search always runs: it ranks in the vector/hybrid modes and supplies the
        # confidence signal in every mode (keyword mode only needs the single best match).
        depth = max(self.config.candidates, top_k)
        vector_limit = 1 if mode == "keyword" else depth
        embedding_holder: dict[str, Any] = {}

        async def vector_branch() -> list[VectorHit]:
            embedding, cached = await _timed(
                timings, "embed", self._query_embedding(tenant_id, query)
            )
            embedding_holder.update(embedding=embedding, cached=cached)
            return await _timed(timings, "vector", self._vector(tenant_id, embedding, vector_limit))

        async def keyword_branch() -> list[KeywordHit]:
            if mode == "vector":
                return []
            return await _timed(timings, "keyword", self._keyword(tenant_id, query, depth))

        vector_hits, keyword_hits = await asyncio.gather(vector_branch(), keyword_branch())
        embedding = embedding_holder["embedding"]
        keyword_scores = {h.chunk_id: h.score for h in keyword_hits}

        fused_scores: dict[uuid.UUID, float] = {}
        fuse_started = time.perf_counter()
        if mode == "vector":
            ranked = [h.chunk_id for h in vector_hits]
        elif mode == "keyword":
            ranked = [h.chunk_id for h in keyword_hits]
        else:
            fused = reciprocal_rank_fusion(
                [[h.chunk_id for h in vector_hits], [h.chunk_id for h in keyword_hits]],
                k=self.config.rrf_k,
            )
            fused_scores = dict(fused)
            ranked = [chunk_id for chunk_id, _ in fused]
            timings["fuse"] = _ms(fuse_started)

        rerank_scores: dict[uuid.UUID, float] = {}
        rerank_applied, rerank_error = False, None
        if mode == "hybrid_rerank":
            pool = ranked[: max(self.config.rerank_candidates, top_k)]
            chunks_by_id = await _timed(
                timings, "hydrate", self._hydrate(tenant_id, pool, embedding)
            )
            pool = [chunk_id for chunk_id in pool if chunk_id in chunks_by_id]
            rerank_started = time.perf_counter()
            try:
                scores = await asyncio.wait_for(
                    self.reranker.rerank(
                        query,
                        [
                            RerankCandidate(chunks_by_id[c].document_title, chunks_by_id[c].content)
                            for c in pool
                        ],
                    ),
                    timeout=self.config.rerank_timeout_seconds,
                )
                if len(scores) != len(pool) or not all(
                    isinstance(s, int | float) and math.isfinite(s) for s in scores
                ):
                    raise ValueError("reranker returned invalid scores")
            except Exception as exc:
                rerank_error = (
                    f"timed out after {self.config.rerank_timeout_seconds:g}s"
                    if isinstance(exc, TimeoutError)
                    else str(exc) or type(exc).__name__
                )
                logger.warning("Reranking failed, keeping fused order: %s", rerank_error)
                ranked = pool
            else:
                rerank_applied = True
                rerank_scores = dict(zip(pool, scores, strict=True))
                # Stable sort: equal scores keep their fused order.
                ranked = sorted(pool, key=lambda c: -rerank_scores[c])
            timings["rerank"] = _ms(rerank_started)
            ranked = ranked[:top_k]
        else:
            ranked = ranked[:top_k]
            chunks_by_id = await _timed(
                timings, "hydrate", self._hydrate(tenant_id, ranked, embedding)
            )

        results = []
        for chunk_id in ranked:
            chunk = chunks_by_id.get(chunk_id)
            if chunk is None:  # deleted between search and hydrate
                continue
            chunk.keyword_score = keyword_scores.get(chunk_id)
            chunk.fused_score = fused_scores.get(chunk_id)
            chunk.rerank_score = rerank_scores.get(chunk_id)
            results.append(chunk)

        top_similarity = vector_hits[0].similarity if vector_hits else None
        threshold = self.relevance_threshold
        timings["total"] = _ms(started)
        return RetrievalResult(
            query=query,
            mode=mode,
            chunks=results,
            top_vector_similarity=top_similarity,
            has_relevant_context=top_similarity is not None and top_similarity >= threshold,
            relevance_threshold=threshold,
            embedding_model=self.embedder.model_name,
            embedding_cached=embedding_holder["cached"],
            reranker=self.reranker.name if mode == "hybrid_rerank" else None,
            rerank_applied=rerank_applied,
            rerank_error=rerank_error,
            timings_ms={k: round(v, 1) for k, v in timings.items()},
        )

    async def _query_embedding(self, tenant_id: uuid.UUID, query: str) -> tuple[list[float], bool]:
        model = self.embedder.model_name
        if self.cache is not None:
            cached = await self.cache.get(model, tenant_id, query)
            if cached is not None and len(cached) == self.embedder.dimensions:
                return cached, True
        embedding = await self.embedder.embed_query(query)
        if self.cache is not None:
            await self.cache.set(model, tenant_id, query, embedding)
        return embedding, False

    async def _vector(
        self, tenant_id: uuid.UUID, embedding: list[float], limit: int
    ) -> list[VectorHit]:
        async with tenant_db(self.sessionmaker, tenant_id) as db:
            return await vector_search(
                db,
                embedding,
                self.embedder.model_name,
                limit,
                iterative_scan=self.config.iterative_scan,
                ef_search=self.config.ef_search,
                exact_fallback=self.config.exact_fallback,
            )

    async def _keyword(self, tenant_id: uuid.UUID, query: str, limit: int) -> list[KeywordHit]:
        async with tenant_db(self.sessionmaker, tenant_id) as db:
            return await keyword_search(db, query, limit)

    async def _hydrate(
        self, tenant_id: uuid.UUID, ids: list[uuid.UUID], embedding: list[float]
    ) -> dict[uuid.UUID, RetrievedChunk]:
        if not ids:
            return {}
        async with tenant_db(self.sessionmaker, tenant_id) as db:
            rows = await db.execute_sql(
                HYDRATE_SQL,
                {"ids": ids, "embedding": embedding, "model": self.embedder.model_name},
            )
            return {
                row.id: RetrievedChunk(
                    chunk_id=row.id,
                    document_id=row.document_id,
                    document_title=row.title,
                    content=row.content,
                    metadata=row.metadata,
                    vector_score=None if row.similarity is None else float(row.similarity),
                )
                for row in rows
            }


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


async def _timed[T](timings: dict[str, float], stage: str, awaitable: Awaitable[T]) -> T:
    started = time.perf_counter()
    try:
        return await awaitable
    finally:
        timings[stage] = _ms(started)
