import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, Float, DateTime, Enum as SAEnum
from sqlalchemy.dialects.mysql import CHAR
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base
import enum


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


def gen_uuid():
    return uuid.uuid4().hex


class GaussianTask(Base):
    __tablename__ = "gaussian_tasks"

    id: Mapped[str] = mapped_column(
        CHAR(32), primary_key=True, default=gen_uuid
    )
    status: Mapped[TaskStatus] = mapped_column(
        SAEnum(TaskStatus), default=TaskStatus.PENDING
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0)

    # File paths
    original_filename: Mapped[str] = mapped_column(String(512))
    input_image_url: Mapped[str] = mapped_column(String(1024))
    output_ply_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    output_spz_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)

    # Stats
    point_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    training_iterations: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Error info
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
