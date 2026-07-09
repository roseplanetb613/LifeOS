"""Chat API — conversation CRUD + LLM proxy with history persistence."""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.chat import Conversation, Message

router = APIRouter(prefix="/chat", tags=["chat"])


# ── Schemas ──

class SendMessageRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(..., min_length=1, max_length=8192)


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    created_at: str


class ConversationResponse(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


# ── Routes ──

@router.post("/send")
async def send_message(
    req: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
):
    """Send message. Returns conversation_id for follow-up WebSocket streaming."""
    if req.conversation_id:
        result = await db.execute(
            select(Conversation).where(Conversation.id == req.conversation_id)
        )
        conv = result.scalar_one_or_none()
        if not conv:
            # Conversation was deleted — start a new one
            req.conversation_id = None
            conv = None
    if not req.conversation_id:
        title = req.message[:80] + ("..." if len(req.message) > 80 else "")
        conv = Conversation(title=title)
        db.add(conv)
        await db.flush()

    user_msg = Message(
        conversation_id=conv.id,
        role="user",
        content=req.message,
        token_count=len(req.message) // 2,  # ~2 chars/token for Chinese
    )
    db.add(user_msg)
    conv.total_tokens = (conv.total_tokens or 0) + user_msg.token_count
    conv.message_count = (conv.message_count or 0) + 1
    conv.updated_at = datetime.utcnow()
    await db.commit()

    return {
        "conversation_id": conv.id,
        "message": req.message,
        "stream": True,
    }


@router.get("/conversations")
async def list_conversations(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """List recent conversations."""
    # Load conversations with message count (avoid lazy loading in async)
    result = await db.execute(
        select(Conversation, func.count(Message.id).label("msg_count"))
        .outerjoin(Message, Message.conversation_id == Conversation.id)
        .group_by(Conversation.id)
        .order_by(Conversation.updated_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = result.all()
    return [
        {
            "id": c.id,
            "title": c.title,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat(),
            "message_count": msg_count,
        }
        for c, msg_count in rows
    ]


@router.get("/conversations/{conv_id}/messages")
async def get_messages(
    conv_id: str,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """Get message history."""
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conv_id)
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    msgs = result.scalars().all()
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        for m in msgs
    ]


@router.delete("/conversations/{conv_id}")
async def delete_conversation(
    conv_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete a conversation."""
    result = await db.execute(
        select(Conversation).where(Conversation.id == conv_id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await db.delete(conv)
    await db.commit()
    return {"ok": True}


@router.delete("/conversations")
async def delete_all_conversations(
    db: AsyncSession = Depends(get_db),
):
    """Delete ALL conversations, messages, and extracted memories."""
    from sqlalchemy import delete
    from app.models.chat import Memory, ExtractionTask
    await db.execute(delete(Message))
    await db.execute(delete(Memory))
    await db.execute(delete(ExtractionTask))
    await db.execute(delete(Conversation))
    await db.commit()
    return {"ok": True, "deleted": "all"}
