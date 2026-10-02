from typing import Annotated

from fastapi import APIRouter, Depends

from app.auth import AdminAuth
from app.retrieval import Retriever, get_retriever
from app.schemas import (
    SearchConfidence,
    SearchHit,
    SearchRequest,
    SearchReranker,
    SearchResponse,
    SearchScores,
)

router = APIRouter(tags=["search"])


@router.post("/search", response_model=SearchResponse)
async def search(
    body: SearchRequest,
    auth: AdminAuth,
    retriever: Annotated[Retriever, Depends(get_retriever)],
) -> SearchResponse:
    """Search the tenant's knowledge base ("test your knowledge base" in the dashboard)."""
    result = await retriever.retrieve(auth.tenant_id, body.query, body.mode, body.top_k)
    return SearchResponse(
        query=result.query,
        mode=result.mode,
        results=[
            SearchHit(
                rank=rank,
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                document_title=chunk.document_title,
                content=chunk.content,
                metadata=chunk.metadata,
                scores=SearchScores(
                    vector=chunk.vector_score,
                    keyword=chunk.keyword_score,
                    fused=chunk.fused_score,
                    rerank=chunk.rerank_score,
                ),
            )
            for rank, chunk in enumerate(result.chunks, start=1)
        ],
        confidence=SearchConfidence(
            top_vector_similarity=result.top_vector_similarity,
            has_relevant_context=result.has_relevant_context,
            threshold=result.relevance_threshold,
        ),
        embedding_model=result.embedding_model,
        embedding_cached=result.embedding_cached,
        reranker=None
        if result.reranker is None
        else SearchReranker(
            name=result.reranker, applied=result.rerank_applied, error=result.rerank_error
        ),
        timings_ms=result.timings_ms,
    )
