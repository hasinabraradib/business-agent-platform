"""Creating documents from uploads, shared by the API and the CLI (same validation and dedupe)."""

import hashlib
from pathlib import PurePath

from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.ingestion.catalog import CatalogMappingError, validate_mapping
from app.ingestion.storage import FileStorage
from app.models import Document
from app.tenancy import TenantDB

UPLOAD_TYPES = {
    ".pdf": "pdf",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".csv": "csv",
}
SUPPORTED = "PDF (.pdf), Markdown (.md), plain text (.txt) or CSV (.csv)"


class UploadRejected(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def validate_upload(filename: str, data: bytes) -> str:
    """Check type, size and encoding; return the source type. Raises UploadRejected."""
    source_type = UPLOAD_TYPES.get(PurePath(filename).suffix.lower())
    if source_type is None:
        raise UploadRejected(415, f"Unsupported file type {filename!r}. Upload {SUPPORTED}.")
    max_bytes = get_settings().max_upload_bytes
    if len(data) > max_bytes:
        raise UploadRejected(413, f"File is larger than the {max_bytes / 1024 / 1024:g} MB limit")
    if not data.strip():
        raise UploadRejected(400, "File is empty")
    if source_type == "pdf" and not data.startswith(b"%PDF-"):
        raise UploadRejected(415, "File is not a valid PDF")
    if source_type != "pdf":
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError:
            message = "Text, Markdown and CSV files must be UTF-8 encoded"
            raise UploadRejected(415, message) from None
    return source_type


async def create_upload_document(
    db: TenantDB,
    storage: FileStorage,
    filename: str,
    data: bytes,
    title: str | None = None,
    catalog_mapping: dict[str, str] | None = None,
) -> tuple[Document, bool]:
    """Create (and commit) a pending document for an upload, or return the existing one.

    Returns (document, created). Identical content for the same tenant is one document.
    """
    filename = PurePath(filename).name
    source_type = validate_upload(filename, data)
    if catalog_mapping is not None:
        if source_type != "csv":
            raise UploadRejected(422, "Only CSV files can be catalogues")
        try:
            catalog_mapping = validate_mapping(catalog_mapping)
        except CatalogMappingError as exc:
            raise UploadRejected(422, str(exc)) from exc
    content_hash = hashlib.sha256(data).hexdigest()
    duplicate = (Document.content_hash == content_hash, Document.source_type != "url")
    if existing := await db.scalar(db.select(Document).where(*duplicate)):
        return existing, False

    document = Document(
        title=(title or "").strip() or PurePath(filename).stem or filename,
        source_type=source_type,
        source_uri=filename,
        content_hash=content_hash,
        catalog_mapping=catalog_mapping,
    )
    db.add(document)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()  # a concurrent upload of the same content won the race
        return await db.scalar(db.select(Document).where(*duplicate)), False

    await storage.save(db.tenant_id, document.id, source_type, data)
    try:
        await db.commit()
    except Exception:
        await storage.delete(db.tenant_id, document.id, source_type)
        raise
    return document, True
