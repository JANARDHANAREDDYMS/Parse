import uuid
from datetime import datetime

from pydantic import BaseModel
from typing import Any


class HealthResponse(BaseModel):
    status: str


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class UploadResponse(BaseModel):
    organization_id: uuid.UUID
    document_id: uuid.UUID
    job_id: uuid.UUID
    document_status: str
    job_status: str
    job_status_url: str


class JobResponse(BaseModel):
    job_id: uuid.UUID
    document_id: uuid.UUID
    job_type: str
    job_status: str
    attempt_number: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    error_message: str | None


class DocumentAnalysisResponse(BaseModel):
    analysis_run_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    status: str
    attempt_number: int
    model: str
    schema_version: str
    preprocessing_schema_version: str
    prompt_version: str
    skill_version: str
    agent_version: str
    started_at: datetime
    completed_at: datetime | None
    input_tokens: int | None
    output_tokens: int | None
    tool_call_count: int | None
    turn_count: int | None
    reported_cost_usd: str | None
    terminal_reason: str | None
    error_code: str | None
    error_message: str | None
    result: dict[str, Any] | None


class TermApplicabilityResponse(BaseModel):
    """Tenant-scoped status and optional validated enrichment result."""
    run_id: uuid.UUID
    document_id: uuid.UUID
    analysis_run_id: uuid.UUID
    status: str
    attempt_number: int
    model: str
    schema_version: str
    prompt_version: str
    skill_version: str
    agent_version: str
    started_at: datetime
    completed_at: datetime | None
    error_code: str | None
    error_message: str | None
    failure_stage: str | None
    diagnostics: dict[str, Any] | None
    result: dict[str, Any] | None


class NormalizationResponse(BaseModel):
    """Tenant-scoped normalization status and safe canonical result."""
    run_id: uuid.UUID
    document_id: uuid.UUID
    analysis_run_id: uuid.UUID
    term_applicability_run_id: uuid.UUID
    status: str
    attempt_number: int
    schema_version: str
    finalization_policy_version: str
    started_at: datetime
    completed_at: datetime | None
    error_code: str | None
    error_stage: str | None
    error_message: str | None
    result: dict[str, Any] | None


class DocumentSummaryResponse(BaseModel):
    """Safe summary row for one tenant-scoped uploaded document."""
    document_id: uuid.UUID
    original_filename: str
    status: str
    created_at: datetime
    updated_at: datetime
    business_status: str | None
    current_stage: str
    item_count: int | None
    review_issue_count: int | None


class DocumentListResponse(BaseModel):
    """Deterministically ordered document summaries."""
    documents: list[DocumentSummaryResponse]


class PipelineResponse(BaseModel):
    """Display-ready pipeline state with safe terminal output only."""
    document_id: uuid.UUID
    preprocessing: dict[str, Any] | None
    document_analysis: dict[str, Any] | None
    sku_mapping: dict[str, Any]
    term_applicability: dict[str, Any] | None
    normalization: dict[str, Any] | None
    document_status: str
