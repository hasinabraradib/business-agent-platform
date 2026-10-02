from fastapi import APIRouter

from app.auth import AdminAuth
from app.models import Document
from app.schemas import DocumentOut

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("", response_model=list[DocumentOut])
async def list_documents(auth: AdminAuth):
    return await auth.db.scalars(auth.db.select(Document).order_by(Document.created_at.desc()))
