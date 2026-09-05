import uuid
from datetime import datetime

from pydantic import BaseModel


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
