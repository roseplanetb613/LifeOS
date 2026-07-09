"""Object storage service — MinIO / S3 compatible."""
from io import BytesIO
from minio import Minio
from app.config import get_settings

settings = get_settings()

_client: Minio | None = None


def get_minio() -> Minio:
    global _client
    if _client is None:
        _client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        # Ensure bucket exists
        if not _client.bucket_exists(settings.minio_bucket):
            _client.make_bucket(settings.minio_bucket)
    return _client


async def upload_to_minio(object_name: str, data: bytes, content_type: str) -> str:
    """Upload file to MinIO, return URL."""
    client = get_minio()
    client.put_object(
        settings.minio_bucket,
        object_name,
        BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
    protocol = "https" if settings.minio_secure else "http"
    return f"{protocol}://{settings.minio_endpoint}/{settings.minio_bucket}/{object_name}"


def get_download_url(object_name: str) -> str:
    """Generate a presigned download URL (valid 1 hour)."""
    client = get_minio()
    return client.presigned_get_object(
        settings.minio_bucket, object_name, expires=3600
    )
