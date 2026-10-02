import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy.exc import IntegrityError

from app.auth import AdminAuth, AuthContext
from app.config import get_settings
from app.ingestion.documents import SUPPORTED, UploadRejected, create_upload_document
from app.ingestion.fetch import Resolver, UnsafeURLError, system_resolver, validate_url
from app.ingestion.queue import JobQueue, get_job_queue
from app.ingestion.storage import FileStorage, get_storage
from app.models import Document
from app.schemas import DocumentFromURL, DocumentOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])


def get_url_resolver() -> Resolver:
    return system_resolver


QueueDep = Annotated[JobQueue, Depends(get_job_queue)]
StorageDep = Annotated[FileStorage, Depends(get_storage)]
ResolverDep = Annotated[Resolver, Depends(get_url_resolver)]


async def _get_document(auth: AuthContext, document_id: uuid.UUID) -> Document:
    document = await auth.db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


async def _enqueue(auth: AuthContext, queue: JobQueue, document: Document) -> None:
    try:
        await queue.enqueue_ingest(auth.tenant_id, document.id)
    except Exception as exc:
        logger.exception("Could not enqueue document %s", document.id)
        document.status = "failed"
        document.error = "could not queue the document for processing; try reingesting it"
        await auth.db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document saved but could not be queued for processing; try reingest",
        ) from exc


async def _existing(auth: AuthContext, *criteria) -> Document | None:
    return await auth.db.scalar(auth.db.select(Document).where(*criteria))


@router.get("", response_model=list[DocumentOut])
async def list_documents(auth: AdminAuth):
    return await auth.db.scalars(auth.db.select(Document).order_by(Document.created_at.desc()))


@router.post(
    "",
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"description": "Identical content was already uploaded; existing document"}},
)
async def upload_document(
    auth: AdminAuth,
    queue: QueueDep,
    storage: StorageDep,
    response: Response,
    file: Annotated[UploadFile, File(description=SUPPORTED)],
    title: Annotated[str | None, Form(max_length=300)] = None,
):
    max_bytes = get_settings().max_upload_bytes
    data = await file.read(max_bytes + 1)  # one byte over is enough to reject
    try:
        document, created = await create_upload_document(
            auth.db, storage, file.filename or "", data, title
        )
    except UploadRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
        return document
    await _enqueue(auth, queue, document)
    return document


@router.post(
    "/url",
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"description": "This URL was already added; existing document"}},
)
async def add_document_from_url(
    body: DocumentFromURL,
    auth: AdminAuth,
    queue: QueueDep,
    resolver: ResolverDep,
    response: Response,
):
    # Checked here for a fast, clear error, and again by the worker on every fetch and redirect.
    try:
        target = await validate_url(body.url, resolver)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    url = str(target.url)

    duplicate = (Document.source_type == "url", Document.source_uri == url)
    if existing := await _existing(auth, *duplicate):
        response.status_code = status.HTTP_200_OK
        return existing

    # The title defaults to the URL; the worker replaces it with the page's <title>.
    document = Document(title=(body.title or "").strip() or url, source_type="url", source_uri=url)
    auth.db.add(document)
    try:
        await auth.db.commit()
    except IntegrityError:
        await auth.db.rollback()
        response.status_code = status.HTTP_200_OK
        return await _existing(auth, *duplicate)
    await _enqueue(auth, queue, document)
    return document


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(document_id: uuid.UUID, auth: AdminAuth):
    return await _get_document(auth, document_id)


@router.post(
    "/{document_id}/reingest", response_model=DocumentOut, status_code=status.HTTP_202_ACCEPTED
)
async def reingest_document(document_id: uuid.UUID, auth: AdminAuth, queue: QueueDep):
    document = await _get_document(auth, document_id)
    # Existing chunks stay searchable until the new ones replace them in one transaction.
    document.status = "pending"
    document.error = None
    await auth.db.commit()
    await _enqueue(auth, queue, document)
    return document


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: uuid.UUID, auth: AdminAuth, storage: StorageDep) -> Response:
    document = await _get_document(auth, document_id)
    source_type = document.source_type
    await auth.db.delete(document)  # chunks go with it (ON DELETE CASCADE)
    await auth.db.commit()
    await storage.delete(auth.tenant_id, document_id, source_type)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
