"""Task & ToolCall models for multi-agent orchestration."""
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Text, Integer, DateTime, ForeignKey, Index, JSON
from sqlalchemy.dialects.mysql import CHAR
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


def gen_id():
    return uuid.uuid4().hex


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True, default=gen_id)
    conversation_id: Mapped[Optional[str]] = mapped_column(CHAR(32), nullable=True)
    user_request: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|planning|executing|done|failed
    plan_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (Index("idx_task_status", "status", "created_at"),)


class TaskStep(Base):
    __tablename__ = "task_steps"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True, default=gen_id)
    task_id: Mapped[str] = mapped_column(CHAR(32), ForeignKey("agent_tasks.id", ondelete="CASCADE"))
    agent_name: Mapped[str] = mapped_column(String(64))
    tool_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    input_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    output_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("idx_step_task", "task_id"),)
