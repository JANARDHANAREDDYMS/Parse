"""Run the combined term-triage and applicability enrichment job.

Mirrors `DocumentAnalysisHandler`/`SkuMappingHandler`'s established shape
(inject persistence, run/validate/persist/reload-validate, leave
`processing_jobs` terminal status to `WorkerRunner`) but tracks a coarse
`stage` -- `persistence` (source loading, run bootstrap, artifact
save/reload), `triage`, `applicability`, `validation`, or `reload_validation`,
defaulting to `unexpected` -- because this handler spans two agents where
either sibling handler only ever has one. On any failure the handler
preserves the exact safe typed error code raised by whichever layer failed
(never a hardcoded generic code) plus whichever agent(s) already returned a
bounded runtime summary, through `TermApplicabilityPersistenceService.mark_run_failed`.
"""
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maximor.config import DatabaseSettings
from maximor.db.models import ProcessingJob
from maximor.document_analysis.errors import DocumentAnalysisError
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.jobs.errors import JobExecutionError
from maximor.jobs.types import JobContext, JobType
from maximor.jobs.normalization import schedule_normalization_if_ready
from maximor.term_applicability.agent import ClaudeTermApplicabilityAgent
from maximor.term_applicability.contracts import build_term_applicability_task, build_selected_term_applicability_task
from maximor.term_applicability.errors import TermApplicabilityError
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
from maximor.term_applicability.tools import PersistedTermApplicabilityTools
from maximor.term_applicability.validation import validate_term_applicability_result
from maximor.term_applicability.versions import TERM_APPLICABILITY_TASK_SCHEMA_VERSION, TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, TERM_APPLICABILITY_PROMPT_VERSION, TERM_APPLICABILITY_SKILL_VERSION, TERM_APPLICABILITY_AGENT_VERSION
from maximor.term_triage.agent import ClaudeTermTriageAgent
from maximor.term_triage.contracts import build_term_triage_task
from maximor.term_triage.errors import TermTriageError
from maximor.term_triage.validation import validate_term_triage_result
from maximor.term_triage.versions import TERM_TRIAGE_TASK_SCHEMA_VERSION
from maximor.document_analysis.repository import DocumentAnalysisRepository
from maximor.document_analysis.tools import PersistedDocumentTools
from maximor.storage import ObjectStorage

logger = structlog.get_logger()


class TermApplicabilityHandler:
    """Execute and persist one combined enrichment attempt; never changes job status."""

    def __init__(self, settings: DatabaseSettings, sessions: async_sessionmaker[AsyncSession], storage: ObjectStorage, analysis_results: DocumentAnalysisPersistenceService, enrichment_results: TermApplicabilityPersistenceService, triage_agent=None, applicability_agent=None) -> None:
        """Inject persistence and optional fake agents for tests."""
        self._settings, self._sessions, self._storage = settings, sessions, storage
        self._analysis, self._enrichment = analysis_results, enrichment_results
        self._triage = triage_agent or ClaudeTermTriageAgent(settings)
        self._applicability = applicability_agent or ClaudeTermApplicabilityAgent(settings)

    async def execute(self, context: JobContext) -> None:
        """Run triage then applicability and persist only fully validated output."""
        if context.job_type != JobType.TERM_APPLICABILITY.value:
            raise JobExecutionError("invalid_job_type", "The term-applicability job type is invalid.")
        run_id = uuid.uuid5(context.processing_job_id, str(context.attempt_number))
        stage = "unexpected"
        triage_runtime = None
        applicability_runtime = None
        try:
            stage = "persistence"
            async with self._sessions() as session:
                job = await session.scalar(select(ProcessingJob).where(ProcessingJob.id == context.processing_job_id, ProcessingJob.organization_id == context.organization_id, ProcessingJob.document_id == context.document_id))
            if job is None or job.analysis_run_id is None:
                raise JobExecutionError("term_applicability_source_unavailable", "The enrichment source is unavailable.")
            try:
                analysis = await self._analysis.load_completed_result(organization_id=context.organization_id, analysis_run_id=job.analysis_run_id)
            except DocumentAnalysisError as exc:
                raise JobExecutionError(exc.code, exc.safe_message) from None
            try:
                await self._enrichment.create_run(run_id=run_id, organization_id=context.organization_id, document_id=context.document_id, analysis_run_id=job.analysis_run_id, processing_job_id=context.processing_job_id, attempt_number=context.attempt_number, schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, prompt_version=TERM_APPLICABILITY_PROMPT_VERSION, skill_version=TERM_APPLICABILITY_SKILL_VERSION, agent_version=TERM_APPLICABILITY_AGENT_VERSION, model=self._settings.term_applicability_model, started_at=datetime.now(UTC))
            except ValueError:
                raise JobExecutionError("term_applicability_source_unavailable", "The enrichment source is unavailable.") from None

            stage = "triage"
            triage_task = build_term_triage_task(result=analysis, analysis_run_id=job.analysis_run_id, schema_version=TERM_TRIAGE_TASK_SCHEMA_VERSION)
            triage_exec = await self._triage.execute(triage_task)
            triage_runtime = triage_exec.runtime
            logger.info(
                "term_triage_runtime",
                mcp_server_status=triage_runtime.mcp_server_status,
                successful_tool_call_order=triage_runtime.successful_tool_call_order,
                tool_call_count=triage_runtime.tool_call_count,
                turn_count=triage_runtime.turn_count,
                correction_attempt_count=triage_runtime.correction_attempt_count,
                terminal_reason=triage_runtime.terminal_reason,
            )
            stage = "validation"
            triage_issues = validate_term_triage_result(triage_task, triage_exec.result)
            if triage_issues:
                raise JobExecutionError("term_triage_validation_failed", "Term triage validation failed.")

            stage = "applicability"
            full = build_term_applicability_task(result=analysis, analysis_run_id=job.analysis_run_id, schema_version=TERM_APPLICABILITY_TASK_SCHEMA_VERSION)
            selected = build_selected_term_applicability_task(full, triage_exec.result)
            document_tools = PersistedDocumentTools(DocumentAnalysisRepository(self._sessions), self._storage)
            tools = PersistedTermApplicabilityTools(document_tools, selected)
            app_exec = await self._applicability.execute(selected, tools)
            applicability_runtime = app_exec.runtime
            logger.info(
                "term_applicability_runtime",
                mcp_server_status=applicability_runtime.mcp_server_status,
                successful_tool_call_order=applicability_runtime.successful_tool_call_order,
                tool_call_count=applicability_runtime.tool_call_count,
                turn_count=applicability_runtime.turn_count,
                correction_attempt_count=applicability_runtime.correction_attempt_count,
                finalization_accepted=applicability_runtime.finalization_accepted,
                terminal_reason=applicability_runtime.terminal_reason,
            )
            stage = "validation"
            issues = validate_term_applicability_result(selected, app_exec.result, applicability_runtime)
            if issues:
                raise JobExecutionError("term_applicability_validation_failed", "Term applicability validation failed.")

            stage = "persistence"
            try:
                await self._enrichment.save_completed_result(run_id=run_id, triage=triage_exec.result, applicability=app_exec.result, runtime=applicability_runtime)
            except ValueError:
                raise JobExecutionError("term_applicability_persistence_failed", "Term applicability persistence could not be completed.") from None

            stage = "reload_validation"
            try:
                loaded = await self._enrichment.load_completed_result(organization_id=context.organization_id, run_id=run_id)
            except ValueError:
                raise JobExecutionError("term_applicability_reload_unavailable", "Enrichment persistence validation failed.") from None
            if loaded.applicability != app_exec.result:
                raise JobExecutionError("term_applicability_reload_mismatch", "Enrichment persistence validation failed.")
            await schedule_normalization_if_ready(self._sessions, organization_id=context.organization_id, document_id=context.document_id, analysis_run_id=job.analysis_run_id, completed_job_id=context.processing_job_id)
        except JobExecutionError as exc:
            await self._fail(run_id, exc.code, exc.safe_message, stage, triage_runtime, applicability_runtime)
            raise
        except (TermTriageError, TermApplicabilityError) as exc:
            recovered = getattr(exc, "runtime", None)
            if stage == "triage" and triage_runtime is None:
                triage_runtime = recovered
            elif stage == "applicability" and applicability_runtime is None:
                applicability_runtime = recovered
            await self._fail(run_id, exc.code, exc.safe_message, stage, triage_runtime, applicability_runtime)
            raise JobExecutionError(exc.code, exc.safe_message) from None
        except Exception as exc:
            await self._fail(run_id, "term_applicability_failed", "Term applicability failed.", stage, triage_runtime, applicability_runtime)
            raise JobExecutionError("term_applicability_failed", "Term applicability failed.") from exc

    async def _fail(self, run_id: uuid.UUID, code: str, message: str, stage: str, triage_runtime=None, applicability_runtime=None) -> None:
        """Best-effort record a safe enrichment-run failure without changing other statuses."""
        try:
            await self._enrichment.mark_run_failed(
                run_id, error_code=code, error_message=message, failure_stage=stage,
                triage_runtime=triage_runtime, applicability_runtime=applicability_runtime,
            )
        except TermApplicabilityError:
            pass
        logger.error(
            "term_applicability_run_failed",
            failure_stage=stage,
            error_code=code,
            triage_terminal_reason=getattr(triage_runtime, "terminal_reason", None),
            triage_correction_attempt_count=getattr(triage_runtime, "correction_attempt_count", None),
            applicability_terminal_reason=getattr(applicability_runtime, "terminal_reason", None),
            applicability_correction_attempt_count=getattr(applicability_runtime, "correction_attempt_count", None),
            applicability_finalization_accepted=getattr(applicability_runtime, "finalization_accepted", None),
        )
