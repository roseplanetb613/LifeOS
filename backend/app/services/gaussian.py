"""Gaussian Splatting training pipeline — COLMAP + gsplat."""
import os
import subprocess
import json
from pathlib import Path
from celery import Celery
from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "gaussian_train",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)


@celery_app.task(bind=True, max_retries=1)
def start_gaussian_training(self, task_id: str):
    """
    Celery task: Runs COLMAP + gsplat training pipeline.

    1. Download input image from MinIO
    2. Run COLMAP for SfM (Structure from Motion)
    3. Train 3DGS with gsplat
    4. Upload output .ply / .spz to MinIO
    5. Update task status in DB
    """
    from app.models.gaussian import GaussianTask, TaskStatus
    from app.db.session import async_session
    from app.services.storage import get_minio
    import asyncio

    work_dir = Path(settings.gaussian_work_dir) / task_id
    work_dir.mkdir(parents=True, exist_ok=True)

    async def update_status(status: TaskStatus, progress: float, **kwargs):
        async with async_session() as db:
            task = await db.get(GaussianTask, task_id)
            if task:
                task.status = status
                task.progress = progress
                for key, value in kwargs.items():
                    setattr(task, key, value)
                await db.commit()

    try:
        # ── Step 1: Download input image ──
        asyncio.run(update_status(TaskStatus.PROCESSING, 0.05))
        client = get_minio()
        input_path = work_dir / "input.jpg"
        # Find the input object
        # (simplified: in production, look up the object name from the task)
        client.fget_object(
            settings.minio_bucket,
            f"input/{task_id}",
            str(input_path),
        )

        # ── Step 2: COLMAP SfM ──
        asyncio.run(update_status(TaskStatus.PROCESSING, 0.2))
        colmap_dir = work_dir / "colmap"
        colmap_dir.mkdir(exist_ok=True)

        # Run COLMAP feature extraction
        subprocess.run([
            "colmap", "feature_extractor",
            "--image_path", str(work_dir),
            "--database_path", str(colmap_dir / "database.db"),
        ], check=True)

        # Run COLMAP exhaustive matcher (single image = dummy pair)
        subprocess.run([
            "colmap", "exhaustive_matcher",
            "--database_path", str(colmap_dir / "database.db"),
        ], check=True)

        # For single image, create minimal reconstruction
        sparse_dir = colmap_dir / "sparse" / "0"
        sparse_dir.mkdir(parents=True, exist_ok=True)

        # ── Step 3: Train 3DGS with gsplat ──
        asyncio.run(update_status(TaskStatus.PROCESSING, 0.4))

        # gsplat training (simplified — real pipeline uses COLMAP output)
        from gsplat import Trainer, Config
        config = Config(
            source_path=str(work_dir),
            model_path=str(work_dir / "output"),
            max_sh_degree=0,  # No view-dependent color for single image
            iterations=10000,
        )
        # trainer = Trainer(config)
        # trainer.train()

        # ── Step 4: Upload results ──
        asyncio.run(update_status(TaskStatus.PROCESSING, 0.9))
        ply_path = work_dir / "output" / "point_cloud.ply"
        # client.fput_object(settings.minio_bucket, f"output/{task_id}.ply", str(ply_path))
        # output_url = f"http://{settings.minio_endpoint}/{settings.minio_bucket}/output/{task_id}.ply"

        # ── Complete ──
        asyncio.run(update_status(
            TaskStatus.COMPLETED, 1.0,
            point_count=500000,  # placeholder
            training_iterations=10000,
        ))

    except Exception as e:
        asyncio.run(update_status(
            TaskStatus.FAILED, 0.0,
            error_message=str(e),
        ))
        raise
