from functools import lru_cache
from pathlib import Path, PurePath

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Application configuration loaded from environment variables or `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MAXIMOR_",
        extra="ignore",
    )

    database_url: SecretStr
    database_echo: bool = False
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    max_pdf_upload_bytes: int = 10 * 1024 * 1024
    local_storage_root: Path = Path(".runtime/documents")
    worker_poll_interval_seconds: float = 1.0
    worker_identity: str = "local-worker"

    @field_validator("local_storage_root")
    @classmethod
    def validate_local_storage_root(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in PurePath(value).parts:
            raise ValueError("local_storage_root must be a safe relative path")
        return value

    @field_validator("max_pdf_upload_bytes")
    @classmethod
    def validate_max_upload_size(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_pdf_upload_bytes must be positive")
        return value

    @field_validator("worker_poll_interval_seconds")
    @classmethod
    def validate_poll_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("worker_poll_interval_seconds must be positive")
        return value


@lru_cache
def get_database_settings() -> DatabaseSettings:
    return DatabaseSettings()
