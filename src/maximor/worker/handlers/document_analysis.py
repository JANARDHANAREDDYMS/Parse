"""Execute one persisted-preprocessing document-analysis job through Claude.

The handler receives a claimed job context, builds trusted narrow-tool scope, saves
validated output, and leaves processing-job terminal status to the worker runner.
"""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maximor.config import DatabaseSettings
from maximor.db.models import Document, DocumentProcessingRun, ProcessingJob
from maximor.document_analysis.agent import ClaudeDocumentAnalysisAgent
from maximor.document_analysis.contracts import DocumentAnalysisRequest
from maximor.document_analysis.errors import DocumentAnalysisError
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.document_analysis.repository import DocumentAnalysisRepository
from maximor.document_analysis.tools import PersistedDocumentTools
from maximor.document_analysis.validation import validate_document_analysis_result
from maximor.document_analysis.versions import DOCUMENT_ANALYSIS_AGENT_VERSION, DOCUMENT_ANALYSIS_PROMPT_VERSION, DOCUMENT_ANALYSIS_SCHEMA_VERSION, ORDER_FORM_ANALYSIS_SKILL_VERSION
from maximor.jobs.errors import JobExecutionError
from maximor.jobs.types import JobContext, JobType
from maximor.storage import ObjectStorage

logger = structlog.get_logger()


class DocumentAnalysisHandler:
    """Run, validate, persist, and reload analysis output without owning job status."""

    def __init__(self, settings: DatabaseSettings, sessions: async_sessionmaker[AsyncSession], storage: ObjectStorage, persistence: DocumentAnalysisPersistenceService, agent: ClaudeDocumentAnalysisAgent | None = None) -> None:
        """Receive explicit storage, persistence, and injectable agent dependencies."""
        self._settings, self._sessions, self._storage = settings, sessions, storage
        self._persistence = persistence
        self._agent = agent or ClaudeDocumentAnalysisAgent(settings)

    async def execute(self, context: JobContext) -> None:
        """Persist one valid analysis result or record a safe failed analysis run."""
        if context.job_type != JobType.DOCUMENT_ANALYSIS.value:
            raise JobExecutionError("invalid_job_type", "The analysis job type is invalid.")
        analysis_run_id = uuid.uuid5(context.processing_job_id, str(context.attempt_number))
        runtime = None
        try:
            async with self._sessions() as session:
                job = await session.scalar(select(ProcessingJob).where(ProcessingJob.id == context.processing_job_id, ProcessingJob.organization_id == context.organization_id, ProcessingJob.document_id == context.document_id))
                document = await session.scalar(select(Document).where(Document.id == context.document_id, Document.organization_id == context.organization_id))
                if job is None or document is None or job.preprocessing_run_id is None:
                    raise JobExecutionError("analysis_source_unavailable", "The analysis source is unavailable.")
                preprocessing = await session.scalar(select(DocumentProcessingRun).where(DocumentProcessingRun.id == job.preprocessing_run_id, DocumentProcessingRun.organization_id == context.organization_id, DocumentProcessingRun.document_id == context.document_id, DocumentProcessingRun.status == "completed"))
                if preprocessing is None:
                    raise JobExecutionError("analysis_preprocessing_unavailable", "The completed preprocessing result is unavailable.")
            request = DocumentAnalysisRequest(organization_id=context.organization_id, document_id=context.document_id, preprocessing_run_id=preprocessing.id, preprocessing_schema_version=preprocessing.schema_version, document_analysis_schema_version=DOCUMENT_ANALYSIS_SCHEMA_VERSION, prompt_version=DOCUMENT_ANALYSIS_PROMPT_VERSION, skill_version=ORDER_FORM_ANALYSIS_SKILL_VERSION, agent_version=DOCUMENT_ANALYSIS_AGENT_VERSION)
            await self._persistence.create_run(run_id=analysis_run_id, organization_id=context.organization_id, document_id=context.document_id, preprocessing_run_id=preprocessing.id, processing_job_id=context.processing_job_id, attempt_number=context.attempt_number, schema_version=DOCUMENT_ANALYSIS_SCHEMA_VERSION, preprocessing_schema_version=preprocessing.schema_version, prompt_version=DOCUMENT_ANALYSIS_PROMPT_VERSION, skill_version=ORDER_FORM_ANALYSIS_SKILL_VERSION, agent_version=DOCUMENT_ANALYSIS_AGENT_VERSION, model=self._settings.document_analysis_model, started_at=datetime.now(UTC))
            tools = PersistedDocumentTools(DocumentAnalysisRepository(self._sessions), self._storage)
            execution = await self._agent.execute(request, tools)
            runtime = execution.runtime
            logger.info(
                "document_analysis_runtime",
                mcp_server_status=runtime.mcp_server_status,
                announced_tool_names=runtime.announced_tool_names,
                successful_tool_call_order=runtime.successful_tool_call_order,
                tool_call_count=runtime.tool_call_count,
                turn_count=runtime.turn_count,
                terminal_reason=runtime.terminal_reason,
            )
            if validate_document_analysis_result(request, execution.result, runtime):
                raise JobExecutionError("analysis_validation_failed", "Document analysis validation failed.")
            await self._persistence.save_completed_result(analysis_run_id=analysis_run_id, result=execution.result, runtime=execution.runtime)
            loaded = await self._persistence.load_completed_result(organization_id=context.organization_id, analysis_run_id=analysis_run_id)
            if loaded != execution.result:
                raise JobExecutionError("analysis_reload_failed", "Document analysis persistence validation failed.")
        except JobExecutionError as exc:
            await self._fail(analysis_run_id, exc.code, exc.safe_message, runtime)
            raise
        except DocumentAnalysisError as exc:
            await self._fail(analysis_run_id, exc.code, exc.safe_message, runtime or getattr(exc, "runtime", None))
            raise JobExecutionError(exc.code, exc.safe_message) from None
        except Exception as exc:
            await self._fail(analysis_run_id, "document_analysis_failed", "Document analysis failed.", runtime)
            raise JobExecutionError("document_analysis_failed", "Document analysis failed.") from exc

    async def _fail(self, run_id: uuid.UUID, code: str, message: str, runtime=None) -> None:
        """Best-effort record a safe analysis-run failure without changing other statuses."""
        try:
            await self._persistence.mark_run_failed(run_id, error_code=code, error_message=message, runtime=runtime)
        except DocumentAnalysisError:
            pass
