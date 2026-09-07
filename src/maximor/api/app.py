import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import structlog
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from maximor.api.schemas import DocumentAnalysisResponse, HealthResponse, JobResponse, UploadResponse, TermApplicabilityResponse, NormalizationResponse
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.document_analysis.errors import DocumentAnalysisError
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
from maximor.normalization.persistence import NormalizationPersistenceService
from maximor.db.models import TermApplicabilityRun
from maximor.db.models import NormalizationRun
from sqlalchemy import select
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
    app.state.analysis_persistence = DocumentAnalysisPersistenceService(
        app.state.session_factory, app.state.storage
    )
    app.state.term_applicability_persistence = TermApplicabilityPersistenceService(
        app.state.session_factory, app.state.storage
    )
    app.state.normalization_persistence = NormalizationPersistenceService(app.state.session_factory, app.state.storage)

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

    @app.get(
        "/v1/organizations/{organization_id}/documents/{document_id}/analysis",
        response_model=DocumentAnalysisResponse,
    )
    async def document_analysis_status(
        request: Request, organization_id: uuid.UUID, document_id: uuid.UUID
    ):
        service = request.app.state.analysis_persistence
        try:
            run = await service.latest_run(
                organization_id=organization_id, document_id=document_id
            )
            if run is None:
                return error_response(404, "analysis_not_found", "Document analysis is unavailable.")
            result = None
            if run.status == "completed":
                result = (await service.load_completed_result(
                    organization_id=organization_id, analysis_run_id=run.id
                )).model_dump(mode="json")
        except DocumentAnalysisError:
            return error_response(503, "analysis_unavailable", "Document analysis is unavailable.")
        return DocumentAnalysisResponse(
            analysis_run_id=run.id, document_id=run.document_id,
            preprocessing_run_id=run.preprocessing_run_id, status=run.status,
            attempt_number=run.attempt_number, model=run.model,
            schema_version=run.schema_version,
            preprocessing_schema_version=run.preprocessing_schema_version,
            prompt_version=run.prompt_version, skill_version=run.skill_version,
            agent_version=run.agent_version, started_at=run.started_at,
            completed_at=run.completed_at, input_tokens=run.input_tokens,
            output_tokens=run.output_tokens, tool_call_count=run.tool_call_count,
            turn_count=run.turn_count,
            reported_cost_usd=str(run.reported_cost_usd) if run.reported_cost_usd is not None else None,
            terminal_reason=run.terminal_reason,
            error_code=run.error_code if run.status == "failed" else None,
            error_message=run.error_message if run.status == "failed" else None,
            result=result,
        )

    @app.get(
        "/v1/organizations/{organization_id}/documents/{document_id}/term-applicability",
        response_model=TermApplicabilityResponse,
    )
    async def term_applicability_status(
        request: Request, organization_id: uuid.UUID, document_id: uuid.UUID
    ):
        try:
            async with request.app.state.session_factory() as session:
                run = await session.scalar(
                    select(TermApplicabilityRun)
                    .where(
                        TermApplicabilityRun.organization_id == organization_id,
                        TermApplicabilityRun.document_id == document_id,
                    )
                    .order_by(TermApplicabilityRun.attempt_number.desc())
                )
            if run is None:
                return error_response(404, "term_applicability_not_found", "Term applicability is unavailable.")
            result = None
            failure_stage = None
            diagnostics = None
            if run.status == "completed":
                result = (await request.app.state.term_applicability_persistence.load_completed_result(
                    organization_id=organization_id, run_id=run.id
                )).model_dump(mode="json")
            elif run.status == "failed":
                diagnostics = run.runtime_diagnostics
                if isinstance(diagnostics, dict):
                    failure_stage = diagnostics.get("failure_stage")
        except Exception:
            return error_response(503, "term_applicability_unavailable", "Term applicability is unavailable.")
        return TermApplicabilityResponse(
            run_id=run.id, document_id=run.document_id, analysis_run_id=run.analysis_run_id,
            status=run.status, attempt_number=run.attempt_number, model=run.model,
            schema_version=run.schema_version, prompt_version=run.prompt_version,
            skill_version=run.skill_version, agent_version=run.agent_version,
            started_at=run.started_at, completed_at=run.completed_at,
            error_code=run.error_code if run.status == "failed" else None,
            error_message=run.error_message if run.status == "failed" else None,
            failure_stage=failure_stage,
            diagnostics=diagnostics,
            result=result,
        )

    @app.get("/v1/organizations/{organization_id}/documents/{document_id}/normalization", response_model=NormalizationResponse)
    async def normalization_status(request: Request, organization_id: uuid.UUID, document_id: uuid.UUID):
        try:
            async with request.app.state.session_factory() as session:
                run = await session.scalar(select(NormalizationRun).where(NormalizationRun.organization_id == organization_id, NormalizationRun.document_id == document_id).order_by(NormalizationRun.attempt_number.desc()))
            if run is None:
                return error_response(404, "normalization_not_found", "Normalization is unavailable.")
            result = None
            if run.status in {"completed", "review_required", "failed_validation"}:
                result = (await request.app.state.normalization_persistence.load_completed_result(organization_id=organization_id, run_id=run.id)).model_dump(mode="json")
        except Exception:
            return error_response(503, "normalization_unavailable", "Normalization is unavailable.")
        return NormalizationResponse(run_id=run.id, document_id=run.document_id, analysis_run_id=run.analysis_run_id, term_applicability_run_id=run.term_applicability_run_id, status=run.status, attempt_number=run.attempt_number, schema_version=run.schema_version, finalization_policy_version=run.finalization_policy_version, started_at=run.started_at, completed_at=run.completed_at, error_code=run.error_code, error_stage=run.error_stage, error_message=run.error_message, result=result)

    return app


app = create_app()
