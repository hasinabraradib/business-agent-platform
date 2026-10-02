import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text

from app.auth import AdminAuth, AuthContext
from app.models import Conversation, Message
from app.schemas import ConversationDetail, ConversationOut, MessageOut

router = APIRouter(prefix="/conversations", tags=["conversations"])


async def _message_counts(auth: AuthContext, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = await auth.db.execute_sql(
        text(
            "SELECT conversation_id, count(*) AS n FROM messages "
            "WHERE tenant_id = :tenant_id AND conversation_id = ANY(:ids) GROUP BY conversation_id"
        ),
        {"ids": ids},
    )
    return {row.conversation_id: row.n for row in rows}


@router.get("", response_model=list[ConversationOut])
async def list_conversations(auth: AdminAuth, limit: int = Query(50, ge=1, le=200)):
    conversations = await auth.db.scalars(
        auth.db.select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
    )
    counts = await _message_counts(auth, [c.id for c in conversations])
    return [
        ConversationOut.model_validate(c).model_copy(update={"message_count": counts.get(c.id, 0)})
        for c in conversations
    ]


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: uuid.UUID, auth: AdminAuth):
    conversation = await auth.db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    messages = await auth.db.scalars(
        auth.db.select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.role.desc())
    )
    return ConversationDetail(
        **ConversationOut.model_validate(conversation).model_dump(exclude={"message_count"}),
        message_count=len(messages),
        messages=[MessageOut.model_validate(m) for m in messages],
    )
