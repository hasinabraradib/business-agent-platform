import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import ApiKeyKind


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TenantOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    settings: dict[str, Any]


class ApiKeyCreate(BaseModel):
    kind: ApiKeyKind
    label: str = Field(default="", max_length=200)


class ApiKeyOut(ORMModel):
    """Key metadata. Never includes the key or its hash."""

    id: uuid.UUID
    kind: str
    prefix: str
    label: str
    created_at: datetime
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="The full API key. Shown only in this response; store it now.")


class DocumentOut(ORMModel):
    id: uuid.UUID
    title: str
    source_type: str
    source_uri: str | None
    status: str
    chunk_count: int
    error: str | None
    content_hash: str | None
    created_at: datetime
    updated_at: datetime


class DocumentFromURL(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=300)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    mode: Literal["vector", "keyword", "hybrid", "hybrid_rerank"] = "hybrid"
    top_k: int = Field(default=5, ge=1, le=50)


class SearchScores(BaseModel):
    vector: float | None = Field(description="Cosine similarity of query and chunk embeddings")
    keyword: float | None = Field(description="idf-weighted keyword score; null if no term matched")
    fused: float | None = Field(description="Reciprocal Rank Fusion score (hybrid modes)")
    rerank: float | None = Field(description="Reranker relevance 0-1 (hybrid_rerank)")


class SearchHit(BaseModel):
    rank: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    content: str
    metadata: dict[str, Any]
    scores: SearchScores


class SearchConfidence(BaseModel):
    top_vector_similarity: float | None
    has_relevant_context: bool
    threshold: float
    strong_keyword_match: bool


class SearchReranker(BaseModel):
    name: str
    applied: bool
    error: str | None


class SearchResponse(BaseModel):
    query: str
    mode: str
    results: list[SearchHit]
    confidence: SearchConfidence
    embedding_model: str
    embedding_cached: bool
    reranker: SearchReranker | None
    timings_ms: dict[str, float]
