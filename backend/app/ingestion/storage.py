"""Local file storage for uploaded documents, laid out as <root>/<tenant_id>/<document_id>.<ext>.

Paths are built only from UUIDs and a fixed extension, never from user-supplied filenames.
"""

import asyncio
import os
import uuid
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

EXTENSIONS = {"pdf": "pdf", "markdown": "md", "text": "txt", "csv": "csv"}


class FileStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, tenant_id: uuid.UUID, document_id: uuid.UUID, source_type: str) -> Path:
        return self.root / str(tenant_id) / f"{document_id}.{EXTENSIONS[source_type]}"

    async def save(
        self, tenant_id: uuid.UUID, document_id: uuid.UUID, source_type: str, data: bytes
    ) -> None:
        path = self.path_for(tenant_id, document_id, source_type)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, path)  # atomic: readers never see a partial file

        await asyncio.to_thread(write)

    async def read(self, tenant_id: uuid.UUID, document_id: uuid.UUID, source_type: str) -> bytes:
        return await asyncio.to_thread(
            self.path_for(tenant_id, document_id, source_type).read_bytes
        )

    async def delete(self, tenant_id: uuid.UUID, document_id: uuid.UUID, source_type: str) -> None:
        if source_type not in EXTENSIONS:
            return  # e.g. URL documents have no stored file
        path = self.path_for(tenant_id, document_id, source_type)
        await asyncio.to_thread(path.unlink, missing_ok=True)


@lru_cache
def get_storage() -> FileStorage:
    return FileStorage(get_settings().storage_dir)
