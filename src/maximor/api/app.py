import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import structlog
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from maximor.api.schemas import DocumentAnalysisResponse, HealthResponse, JobResponse, UploadResponse, TermApplicabilityResponse, NormalizationResponse, DocumentListResponse, DocumentSummaryResponse, PipelineResponse
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.document_analysis.errors import DocumentAnalysisError
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
from maximor.normalization.persistence import NormalizationPersistenceService
from maximor.db.models import TermApplicabilityRun, NormalizationRun, Document, ProcessingJob, DocumentProcessingRun, DocumentAnalysisRun, SkuMappingRun, DocumentProductCandidate, NormalizedLineItemProjection, NormalizationIssueProjection
from sqlalchemy import select, func
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
    app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in configured.cors_allowed_origins.split(",") if x.strip()], allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"])
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

    @app.get("/v1/organizations/{organization_id}/documents", response_model=DocumentListResponse)
    async def document_list(request: Request, organization_id: uuid.UUID):
        """Return safe newest-first summaries for one organization."""
        try:
            async with request.app.state.session_factory() as session:
                docs = (await session.scalars(select(Document).where(Document.organization_id == organization_id).order_by(Document.created_at.desc(), Document.id.desc()))).all()
                runs = (await session.scalars(select(NormalizationRun).where(NormalizationRun.organization_id == organization_id))).all()
                jobs = (await session.scalars(select(ProcessingJob).where(ProcessingJob.organization_id == organization_id))).all()
                line_counts = dict((row[0], row[1]) for row in (await session.execute(select(NormalizedLineItemProjection.normalization_run_id, func.count()).group_by(NormalizedLineItemProjection.normalization_run_id))).all())
                issue_counts = dict((row[0], row[1]) for row in (await session.execute(select(NormalizationIssueProjection.normalization_run_id, func.count()).group_by(NormalizationIssueProjection.normalization_run_id))).all())
            latest = {}
            for run in runs:
                if run.document_id not in latest or run.attempt_number > latest[run.document_id].attempt_number: latest[run.document_id] = run
            items=[]
            for doc in docs:
                related=[j for j in jobs if j.document_id == doc.id]
                run=latest.get(doc.id)
                stage = "Queued"
                if related:
                    current=max(related, key=lambda j: (j.created_at, j.attempt_number))
                    stage={"document_preprocessing":"PDF preprocessing","document_analysis":"Document analysis","sku_mapping":"SKU mapping","term_applicability":"Term enrichment","normalization":"Normalization / finalization"}.get(current.job_type, current.job_type)
                terminal = run is not None and run.status in {"completed", "review_required", "failed_validation"}
                items.append(DocumentSummaryResponse(document_id=doc.id, original_filename=doc.original_filename, status=doc.status, created_at=doc.created_at, updated_at=doc.updated_at, business_status=run.status if run else None, current_stage=stage, item_count=int(line_counts.get(run.id, 0)) if terminal else None, review_issue_count=int(issue_counts.get(run.id, 0)) if terminal else None))
            return DocumentListResponse(documents=items)
        except SQLAlchemyError:
            return error_response(503, "documents_unavailable", "Documents are unavailable.")

    @app.get("/v1/organizations/{organization_id}/documents/{document_id}/pipeline", response_model=PipelineResponse)
    async def document_pipeline(request: Request, organization_id: uuid.UUID, document_id: uuid.UUID):
        """Return a tenant-scoped display projection of pipeline state."""
        try:
            async with request.app.state.session_factory() as session:
                doc=await session.scalar(select(Document).where(Document.organization_id==organization_id, Document.id==document_id))
                jobs=(await session.scalars(select(ProcessingJob).where(ProcessingJob.organization_id==organization_id, ProcessingJob.document_id==document_id).order_by(ProcessingJob.created_at))).all()
                pre=await session.scalar(select(DocumentProcessingRun).where(DocumentProcessingRun.organization_id==organization_id,DocumentProcessingRun.document_id==document_id).order_by(DocumentProcessingRun.attempt_number.desc()))
                ana=await session.scalar(select(DocumentAnalysisRun).where(DocumentAnalysisRun.organization_id==organization_id,DocumentAnalysisRun.document_id==document_id).order_by(DocumentAnalysisRun.attempt_number.desc()))
                term=await session.scalar(select(TermApplicabilityRun).where(TermApplicabilityRun.organization_id==organization_id,TermApplicabilityRun.document_id==document_id).order_by(TermApplicabilityRun.attempt_number.desc()))
                norm=await session.scalar(select(NormalizationRun).where(NormalizationRun.organization_id==organization_id,NormalizationRun.document_id==document_id).order_by(NormalizationRun.attempt_number.desc()))
            if doc is None: return error_response(404,"document_not_found","The document was not found.")
            by_type={}
            for job in jobs: by_type.setdefault(job.job_type,[]).append(job)
            sku=[{"job_id":j.id,"status":j.status,"candidate_id":j.document_product_candidate_id,"error_code":j.error_code} for j in by_type.get("sku_mapping",[])]
            pipeline=lambda run: ({"status":run.status,"started_at":run.started_at,"completed_at":run.completed_at,"error_code":run.error_code} if run else None)
            norm_data=pipeline(norm)
            if norm and norm.status in {"completed","review_required","failed_validation"}:
                try: norm_data["result"]=(await request.app.state.normalization_persistence.load_completed_result(organization_id=organization_id,run_id=norm.id)).model_dump(mode="json")
                except Exception: pass
            return PipelineResponse(document_id=document_id, preprocessing=pipeline(pre), document_analysis=pipeline(ana), sku_mapping={"total":len(sku),"completed":sum(x["status"]=="completed" for x in sku),"failed":sum(x["status"]=="failed" for x in sku),"review":0,"candidates":sku}, term_applicability=pipeline(term), normalization=norm_data, document_status=doc.status)
        except SQLAlchemyError:
            return error_response(503,"pipeline_unavailable","Pipeline status is unavailable.")

    return app


app = create_app()
