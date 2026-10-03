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
    catalog_mapping: dict[str, Any] | None = None
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


MAX_CHAT_MESSAGE_CHARS = 2000


class ChatRequestBody(BaseModel):
    conversation_id: uuid.UUID | None = None
    # Chosen by the widget (a random id kept in the visitor's browser).
    visitor_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    message: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_CHARS)
    stream: bool = True
    # Set by the widget per customer message; resending the same id never stores it twice.
    client_message_id: str | None = Field(
        default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )


class Citation(BaseModel):
    marker: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    metadata: dict[str, Any]
    snippet: str


class ChatResponseBody(BaseModel):
    conversation_id: uuid.UUID
    message_id: uuid.UUID | None
    reply: str
    outcome: str
    citations: list[Citation]
    usage: dict[str, int]
    timings: dict[str, float]
    retrieval: dict[str, Any] | None
    model: str | None = None
    replayed: bool = False  # a retry of an already answered client_message_id
    error: str | None = None


class ConversationOut(ORMModel):
    id: uuid.UUID
    channel: str
    visitor_id: str
    status: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class MessageOut(ORMModel):
    id: uuid.UUID
    role: str
    content: str
    citations: list[dict[str, Any]]
    outcome: str | None
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    timings: dict[str, Any]
    retrieval: dict[str, Any] | None
    error: str | None
    client_message_id: str | None
    in_reply_to: uuid.UUID | None
    created_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]
