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
