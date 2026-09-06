from functools import lru_cache
from pathlib import Path, PurePath

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DatabaseSettings(BaseSettings):
    """Application configuration loaded from environment variables or `.env`."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="MAXIMOR_",
        extra="ignore",
    )

    database_url: SecretStr
    database_echo: bool = False
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    max_pdf_upload_bytes: int = 10 * 1024 * 1024
    # Resolve this relative value against PROJECT_ROOT so root/src launches agree.
    local_storage_root: Path = Path("src/.runtime/documents")
    worker_poll_interval_seconds: float = 1.0
    worker_identity: str = "local-worker"
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "MAXIMOR_ANTHROPIC_API_KEY"),
    )
    document_analysis_model: str = Field(
        default="claude-sonnet-5",
        validation_alias=AliasChoices("DOCUMENT_ANALYSIS_MODEL", "MAXIMOR_DOCUMENT_ANALYSIS_MODEL"),
    )
    document_analysis_max_turns: int = Field(
        default=12, ge=1, le=100,
        validation_alias=AliasChoices("DOCUMENT_ANALYSIS_MAX_TURNS", "MAXIMOR_DOCUMENT_ANALYSIS_MAX_TURNS"),
    )
    document_analysis_max_thinking_tokens: int = Field(
        default=4096, ge=256, le=32_000,
        validation_alias=AliasChoices("DOCUMENT_ANALYSIS_MAX_THINKING_TOKENS", "MAXIMOR_DOCUMENT_ANALYSIS_MAX_THINKING_TOKENS"),
    )
    document_analysis_timeout_seconds: float = Field(
        default=120, gt=0, le=900,
        validation_alias=AliasChoices("DOCUMENT_ANALYSIS_TIMEOUT_SECONDS", "MAXIMOR_DOCUMENT_ANALYSIS_TIMEOUT_SECONDS"),
    )
    document_analysis_max_budget_usd: float | None = Field(
        default=None, gt=0, le=100,
        validation_alias=AliasChoices("DOCUMENT_ANALYSIS_MAX_BUDGET_USD", "MAXIMOR_DOCUMENT_ANALYSIS_MAX_BUDGET_USD"),
    )
    document_analysis_max_corrections: int = Field(
        default=1, ge=0, le=1,
        validation_alias=AliasChoices("DOCUMENT_ANALYSIS_MAX_CORRECTIONS", "MAXIMOR_DOCUMENT_ANALYSIS_MAX_CORRECTIONS"),
    )
    document_analysis_project_root: Path = Path(".")
    sku_mapping_model: str = Field(
        default="claude-sonnet-5",
        validation_alias=AliasChoices("SKU_MAPPING_MODEL", "MAXIMOR_SKU_MAPPING_MODEL"),
    )
    sku_mapping_max_turns: int = Field(
        default=8, ge=1, le=100,
        validation_alias=AliasChoices("SKU_MAPPING_MAX_TURNS", "MAXIMOR_SKU_MAPPING_MAX_TURNS"),
    )
    sku_mapping_max_thinking_tokens: int = Field(
        default=2048, ge=256, le=32_000,
        validation_alias=AliasChoices("SKU_MAPPING_MAX_THINKING_TOKENS", "MAXIMOR_SKU_MAPPING_MAX_THINKING_TOKENS"),
    )
    sku_mapping_timeout_seconds: float = Field(
        default=60, gt=0, le=900,
        validation_alias=AliasChoices("SKU_MAPPING_TIMEOUT_SECONDS", "MAXIMOR_SKU_MAPPING_TIMEOUT_SECONDS"),
    )
    sku_mapping_max_budget_usd: float | None = Field(
        default=None, gt=0, le=100,
        validation_alias=AliasChoices("SKU_MAPPING_MAX_BUDGET_USD", "MAXIMOR_SKU_MAPPING_MAX_BUDGET_USD"),
    )
    sku_mapping_max_corrections: int = Field(
        default=1, ge=0, le=1,
        validation_alias=AliasChoices("SKU_MAPPING_MAX_CORRECTIONS", "MAXIMOR_SKU_MAPPING_MAX_CORRECTIONS"),
    )
    sku_mapping_project_root: Path = Path(".")

    @field_validator("local_storage_root")
    @classmethod
    def validate_local_storage_root(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in PurePath(value).parts:
            raise ValueError("local_storage_root must be a safe relative path")
        return (PROJECT_ROOT / value).resolve()

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
