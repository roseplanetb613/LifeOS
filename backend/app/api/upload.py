"""Photo upload API — triggers 3DGS training pipeline."""
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db.session import get_db
from app.models.gaussian import GaussianTask, TaskStatus
from app.services.gaussian import start_gaussian_training
from app.services.storage import upload_to_minio

router = APIRouter(prefix="/gaussian", tags=["gaussian"])

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/heic", "image/webp"}
MAX_SIZE_MB = 50


class UploadResponse(BaseModel):
    task_id: str
    status: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    progress: float
    output_ply_url: str | None = None
    output_spz_url: str | None = None
    point_count: int | None = None
    error_message: str | None = None


@router.post("/upload", response_model=UploadResponse)
async def upload_photo(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Upload a photo and start 3DGS training."""
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(400, f"Unsupported type: {file.content_type}")

    contents = await file.read()
    if len(contents) > MAX_SIZE_MB * 1024 * 1024:
        raise HTTPException(400, f"File too large (max {MAX_SIZE_MB}MB)")

    # Upload to MinIO
    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    import uuid
    object_name = f"input/{uuid.uuid4().hex}.{ext}"
    input_url = await upload_to_minio(object_name, contents, file.content_type)

    # Create task
    task = GaussianTask(
        original_filename=file.filename,
        input_image_url=input_url,
        status=TaskStatus.PENDING,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    # Dispatch Celery
    start_gaussian_training.delay(task.id)

    return UploadResponse(task_id=task.id, status=task.status.value)


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Poll training task status."""
    result = await db.execute(
        select(GaussianTask).where(GaussianTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")

    return TaskStatusResponse(
        task_id=task.id,
        status=task.status.value,
        progress=task.progress,
        output_ply_url=task.output_ply_url,
        output_spz_url=task.output_spz_url,
        point_count=task.point_count,
        error_message=task.error_message,
    )


@router.get("/tasks")
async def list_tasks(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """List recent training tasks."""
    result = await db.execute(
        select(GaussianTask)
        .order_by(GaussianTask.created_at.desc())
        .limit(limit)
    )
    tasks = result.scalars().all()
    return [
        {
            "task_id": t.id,
            "status": t.status.value,
            "progress": t.progress,
            "output_ply_url": t.output_ply_url,
            "output_spz_url": t.output_spz_url,
            "point_count": t.point_count,
            "error_message": t.error_message,
        }
        for t in tasks
    ]
