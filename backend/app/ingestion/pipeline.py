"""The ingestion job: parse -> chunk -> embed -> store, then mark the document ready or failed.

Runs as the application role with the job's tenant bound to each transaction, so RLS applies to
the worker exactly as it does to the API. Slow work (fetching, parsing, embedding) happens
outside any transaction; the chunk swap is one transaction holding a row lock on the document,
so readers see either the old chunks or the new ones, never a mix.
"""

import hashlib
import logging
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.embeddings import EmbeddingError, EmbeddingInput, EmbeddingProvider
from app.ingestion.chunking import chunk_document
from app.ingestion.fetch import FetchedPage, FetchError, UnsafeURLError, fetch_url, parse_fetched
from app.ingestion.parsers import ParsedDocument, ParseError, parse
from app.ingestion.storage import FileStorage
from app.models import Chunk, Document
from app.tenancy import tenant_db

logger = logging.getLogger(__name__)

# Errors whose messages are written for tenants; anything else gets a generic message.
EXPECTED_ERRORS = (ParseError, FetchError, UnsafeURLError, EmbeddingError)
INTERNAL_ERROR = "internal error while processing the document; try reingesting it"


async def default_fetch(url: str) -> FetchedPage:
    settings = get_settings()
    return await fetch_url(
        url,
        max_bytes=settings.url_fetch_max_bytes,
        timeout_seconds=settings.url_fetch_timeout_seconds,
    )


@dataclass
class IngestDeps:
    sessionmaker: async_sessionmaker[AsyncSession]  # application role
    embedder: EmbeddingProvider
    storage: FileStorage
    fetch: Callable[[str], Awaitable[FetchedPage]] = default_fetch


@dataclass
class _Prepared:
    rows: list[Chunk]
    title: str | None = None  # a better title found in the source (web pages)
    content_hash: str | None = None  # for URLs: hash of the fetched body


async def process_document(deps: IngestDeps, tenant_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """Ingest one document. Returns the final status ('ready', 'failed' or 'missing')."""
    async with tenant_db(deps.sessionmaker, tenant_id) as db:
        document = await db.get(Document, document_id)
        if document is None:
            logger.info("Document %s not found for tenant %s; skipping", document_id, tenant_id)
            return "missing"
        document.status = "processing"
        document.error = None
        snapshot = (document.source_type, document.source_uri, document.title)
        await db.commit()

    source_type, source_uri, title = snapshot
    try:
        prepared = await _prepare(deps, tenant_id, document_id, source_type, source_uri, title)
    except EXPECTED_ERRORS as exc:
        return await _mark_failed(deps, tenant_id, document_id, str(exc))
    except FileNotFoundError:
        return await _mark_failed(deps, tenant_id, document_id, "the uploaded file is missing")
    except Exception:
        logger.exception("Unexpected error preparing document %s", document_id)
        return await _mark_failed(deps, tenant_id, document_id, INTERNAL_ERROR)

    try:
        async with tenant_db(deps.sessionmaker, tenant_id) as db:
            document = await db.get(Document, document_id, for_update=True)
            if document is None:
                return "missing"  # deleted while we were working
            await db.delete_where(Chunk, Chunk.document_id == document_id)
            db.add_all(prepared.rows)
            document.status = "ready"
            document.error = None
            document.chunk_count = len(prepared.rows)
            if prepared.title and document.title == document.source_uri:
                document.title = prepared.title  # replace the URL placeholder title
            if prepared.content_hash:
                document.content_hash = prepared.content_hash
            await db.commit()
    except Exception:
        # The swap rolled back as a whole: the previous chunks are still in place.
        logger.exception("Storing chunks failed for document %s", document_id)
        return await _mark_failed(deps, tenant_id, document_id, INTERNAL_ERROR)
    return "ready"


async def _prepare(
    deps: IngestDeps,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    source_type: str,
    source_uri: str | None,
    title: str,
) -> _Prepared:
    content_hash = None
    if source_type == "url":
        if not source_uri:
            raise ParseError("document has no URL")
        page = await deps.fetch(source_uri)
        parsed: ParsedDocument = parse_fetched(page)
        content_hash = hashlib.sha256(page.body).hexdigest()
    else:
        parsed = parse(source_type, await deps.storage.read(tenant_id, document_id, source_type))

    embed_title = parsed.title if source_type == "url" and parsed.title else title
    drafts = chunk_document(parsed, source=source_uri)
    if not drafts:
        raise ParseError("the document contains no text to index")

    vectors = await deps.embedder.embed_documents(
        [EmbeddingInput(text=d.embedding_text(), title=embed_title) for d in drafts]
    )
    if len(vectors) != len(drafts):
        raise EmbeddingError("embedding provider returned the wrong number of vectors")
    for vector in vectors:
        if len(vector) != deps.embedder.dimensions or not all(map(math.isfinite, vector)):
            raise EmbeddingError("embedding provider returned an invalid vector")

    rows = [
        Chunk(
            tenant_id=tenant_id,
            document_id=document_id,
            chunk_index=draft.index,
            content=draft.content,
            meta=draft.metadata,
            embedding=vector,
            embedding_model=deps.embedder.model_name,
        )
        for draft, vector in zip(drafts, vectors, strict=True)
    ]
    return _Prepared(rows=rows, title=parsed.title, content_hash=content_hash)


async def _mark_failed(
    deps: IngestDeps, tenant_id: uuid.UUID, document_id: uuid.UUID, message: str
) -> str:
    async with tenant_db(deps.sessionmaker, tenant_id) as db:
        document = await db.get(Document, document_id, for_update=True)
        if document is None:
            return "missing"
        document.status = "failed"
        document.error = message[:1000]
        await db.commit()
    logger.info("Document %s failed: %s", document_id, message)
    return "failed"
