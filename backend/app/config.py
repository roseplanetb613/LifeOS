from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    secret_key: str = "change-me"

    # Database
    database_url: str = "mysql+aiomysql://lifeos:lifeos@localhost:3306/lifeos"
    database_url_sync: str = "mysql+pymysql://lifeos:lifeos@localhost:3306/lifeos"

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # LLM
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_max_tokens: int = 4096

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "gaussian-files"
    minio_secure: bool = False

    # Ollama (local LLM + embedding)
    ollama_base_url: str = "http://localhost:11434"
    ollama_embed_model: str = "bge-m3"

    # Gaussian Training
    gaussian_work_dir: str = "/data/gaussian-train"
    gaussian_gpu_memory: int = 20000
    gaussian_max_points: int = 1_000_000

    model_config = dict(env_file=".env", env_file_encoding="utf-8")


@lru_cache()
def get_settings() -> Settings:
    return Settings()
