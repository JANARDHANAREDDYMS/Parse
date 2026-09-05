import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import structlog
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from maximor.api.schemas import HealthResponse, JobResponse, UploadResponse
from maximor.config import DatabaseSettings, get_database_settings
from maximor.db.health import check_database_health
from maximor.db.session import get_session_factory
from maximor.jobs.service import (
    DuplicateDocumentError,
    OrganizationNotFoundError,
    create_document_job,
    get_tenant_job,
)
from maximor.jobs.types import JobType
from maximor.storage import LocalObjectStorage, StorageError

logger = structlog.get_logger()


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


def create_app(
    *,
    settings: DatabaseSettings | None = None,
    storage: LocalObjectStorage | None = None,
    session_factory=None,
) -> FastAPI:
    configured = settings or get_database_settings()
    app = FastAPI(title="Maximor AI", version="0.1.0")
    app.state.settings = configured
    app.state.storage = storage or LocalObjectStorage(configured.local_storage_root)
    app.state.session_factory = session_factory or get_session_factory()

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        if any(
            error.get("type") == "missing" and "file" in error.get("loc", ())
            for error in errors
        ):
            return error_response(422, "missing_file", "A PDF file is required.")
        if any("uuid" in error.get("type", "") for error in errors):
            return error_response(
                422, "invalid_uuid", "A path identifier is not a valid UUID."
            )
        return error_response(422, "invalid_request", "The request is invalid.")

    @app.get("/health/live", response_model=HealthResponse)
    async def live() -> HealthResponse:
        return HealthResponse(status="live")

    @app.get("/health/ready", response_model=HealthResponse)
    async def ready(request: Request):
        try:
            health = await check_database_health(request.app.state.session_factory.kw["bind"])
        except Exception:
            return error_response(503, "database_unavailable", "The database is unavailable.")
        if not health.connected:
            return error_response(503, "database_unavailable", "The database is unavailable.")
        return HealthResponse(status="ready")

    @app.post(
        "/v1/organizations/{organization_id}/documents",
        status_code=202,
        response_model=UploadResponse,
    )
    async def upload_document(
        request: Request,
        organization_id: uuid.UUID,
        file: UploadFile = File(...),
    ):
        if file.content_type != "application/pdf":
            return error_response(
                415,
                "unsupported_media_type",
                "Only application/pdf uploads are accepted.",
            )
        header = await file.read(5)
        if not header:
            return error_response(400, "empty_upload", "The uploaded PDF is empty.")
        if header != b"%PDF-":
            return error_response(
                415,
                "invalid_pdf_signature",
                "The upload does not have a valid PDF signature.",
            )

        async def chunks() -> AsyncIterator[bytes]:
            yield header
            while chunk := await file.read(1024 * 1024):
                yield chunk

        filename = Path(file.filename or "upload.pdf").name[:512] or "upload.pdf"
        try:
            result = await create_document_job(
                session_factory=request.app.state.session_factory,
                storage=request.app.state.storage,
                organization_id=organization_id,
                original_filename=filename,
                media_type="application/pdf",
                chunks=chunks(),
                maximum_bytes=request.app.state.settings.max_pdf_upload_bytes,
                job_type=JobType.DOCUMENT_PREPROCESSING,
            )
        except OrganizationNotFoundError:
            return error_response(
                404, "organization_not_found", "The organization was not found."
            )
        except DuplicateDocumentError:
            return error_response(
                409,
                "duplicate_document",
                "This document was already uploaded for the organization.",
            )
        except StorageError as exc:
            status = 413 if exc.code == "upload_too_large" else 400
            return error_response(status, exc.code, exc.message)
        except SQLAlchemyError:
            logger.error("document_persistence_failed", organization_id=str(organization_id))
            return error_response(503, "database_unavailable", "The document could not be persisted.")
        finally:
            await file.close()

        return UploadResponse(
            **result.__dict__,
            job_status_url=f"/v1/organizations/{organization_id}/jobs/{result.job_id}",
        )

    @app.get(
        "/v1/organizations/{organization_id}/jobs/{job_id}",
        response_model=JobResponse,
    )
    async def job_status(
        request: Request, organization_id: uuid.UUID, job_id: uuid.UUID
    ):
        try:
            job = await get_tenant_job(request.app.state.session_factory, organization_id, job_id)
        except SQLAlchemyError:
            return error_response(503, "database_unavailable", "The database is unavailable.")
        if job is None:
            return error_response(404, "job_not_found", "The job was not found.")
        return JobResponse(
            job_id=job.id,
            document_id=job.document_id,
            job_type=job.job_type,
            job_status=job.status,
            attempt_number=job.attempt_number,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            error_code=job.error_code if job.status == "failed" else None,
            error_message=job.error_message if job.status == "failed" else None,
        )

    return app


app = create_app()
