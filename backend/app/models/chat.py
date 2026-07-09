"""Chat domain models — conversations, messages, memories, extraction tasks."""
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Text, Integer, DateTime, ForeignKey, Index, JSON
from sqlalchemy.dialects.mysql import CHAR, TINYINT
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.session import Base


def gen_id():
    return uuid.uuid4().hex


# ══════════════════════════════════════════
# 1. Conversation
# ══════════════════════════════════════════
class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True, default=gen_id)
    title: Mapped[str] = mapped_column(String(256), default="New Chat")
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    message_count: Mapped[int] = mapped_column(Integer, default=0)
    user_msg_count: Mapped[int] = mapped_column(Integer, default=0)
    ai_msg_count: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)

    tags: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    is_pinned: Mapped[bool] = mapped_column(TINYINT(1), default=False)
    is_archived: Mapped[bool] = mapped_column(TINYINT(1), default=False)

    last_extracted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at"
    )


# ══════════════════════════════════════════
# 2. Message
# ══════════════════════════════════════════
class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True, default=gen_id)
    conversation_id: Mapped[str] = mapped_column(CHAR(32), ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16), default="text")
    embedding_status: Mapped[str] = mapped_column(String(16), default="none")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")

    __table_args__ = (
        Index("idx_msg_conv_time", "conversation_id", "created_at"),
        Index("idx_msg_embedding", "embedding_status", "created_at"),
    )


# ══════════════════════════════════════════
# 3. Memory (long-term, extracted facts)
# ══════════════════════════════════════════
class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True, default=gen_id)
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # preference|project|fact|plan
    base_importance: Mapped[int] = mapped_column(Integer, default=50)  # 0-100, LLM original
    confidence: Mapped[int] = mapped_column(Integer, default=50)  # 0-100, dynamic (attenuation applied)
    confirmations: Mapped[int] = mapped_column(Integer, default=1)  # times re-confirmed
    keywords: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    last_confirmed: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_faded: Mapped[bool] = mapped_column(TINYINT(1), default=False)

    source_conv_id: Mapped[Optional[str]] = mapped_column(CHAR(32), nullable=True)
    source_msg_ids: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    chroma_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    chroma_synced: Mapped[bool] = mapped_column(TINYINT(1), default=False)

    is_corrected: Mapped[bool] = mapped_column(TINYINT(1), default=False)
    corrected_by: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(TINYINT(1), default=False)
    conflicts_with: Mapped[Optional[str]] = mapped_column(CHAR(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_mem_category", "category", "confidence"),
        Index("idx_mem_chroma", "chroma_synced"),
        Index("idx_mem_confidence", "confidence"),
    )


# ══════════════════════════════════════════
# 4. Extraction Task
# ══════════════════════════════════════════
class ExtractionTask(Base):
    __tablename__ = "extraction_tasks"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True, default=gen_id)
    conversation_id: Mapped[str] = mapped_column(CHAR(32), ForeignKey("conversations.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    extracted_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("idx_ext_status", "status", "created_at"),)
