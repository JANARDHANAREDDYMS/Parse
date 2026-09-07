"""Execute one candidate's SKU-mapping job through Claude.

The handler receives a claimed job context, loads the tenant-scoped analysis
result and candidate, builds the existing `SkuMappingTask`, runs the existing
retriever/tools/agent, validates, persists, and reload-validates the result —
leaving processing-job terminal status to the worker runner, exactly like
`DocumentAnalysisHandler`.
"""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maximor.config import DatabaseSettings
from maximor.db.models import DocumentProductCandidate, ProcessingJob
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.document_analysis.repository import DocumentAnalysisRepository
from maximor.document_analysis.tools import PersistedDocumentTools
from maximor.jobs.errors import JobExecutionError
from maximor.jobs.types import JobContext, JobType
from maximor.jobs.normalization import schedule_normalization_if_ready
from maximor.sku_mapping.agent import ClaudeSkuMappingAgent
from maximor.sku_mapping.contracts import SkuMappingRunArtifact, build_sku_mapping_task
from maximor.sku_mapping.errors import SkuMappingError, SkuMappingTaskConstructionError
from maximor.sku_mapping.persistence import SkuMappingPersistenceService
from maximor.sku_mapping.repository import PostgresSkuRepository
from maximor.sku_mapping.retrieval import DeterministicHybridSkuRetriever
from maximor.sku_mapping.tools import PersistedSkuMappingTools
from maximor.sku_mapping.versions import (
    HYBRID_SKU_RETRIEVER_VERSION,
    SKU_MAPPING_AGENT_VERSION,
    SKU_MAPPING_ARTIFACT_SCHEMA_VERSION,
    SKU_MAPPING_DECISION_SCHEMA_VERSION,
    SKU_MAPPING_PROMPT_VERSION,
    SKU_MAPPING_SKILL_VERSION,
    SKU_MAPPING_TASK_SCHEMA_VERSION,
)
from maximor.storage import ObjectStorage

logger = structlog.get_logger()


class SkuMappingHandler:
    """Run, validate, persist, and reload one candidate's SKU-mapping decision.

    A successfully validated MATCH, NO_MATCH, or AMBIGUOUS decision is a
    *completed* job — these are business outcomes the agent is explicitly
    designed to reach, not technical failures. Only a genuine
    agent/persistence failure (a decision could not be reached or safely
    recorded at all) raises `JobExecutionError`.
    """

    def __init__(
        self,
        settings: DatabaseSettings,
        sessions: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        analysis_results: DocumentAnalysisPersistenceService,
        mapping_results: SkuMappingPersistenceService,
        agent: ClaudeSkuMappingAgent | None = None,
    ) -> None:
        """Receive explicit storage, persistence, and an injectable agent dependency."""
        self._settings, self._sessions, self._storage = settings, sessions, storage
        self._analysis_results = analysis_results
        self._mapping_results = mapping_results
        self._agent = agent or ClaudeSkuMappingAgent(settings)

    async def execute(self, context: JobContext) -> None:
        """Persist one valid SKU-mapping decision or record a safe failed run."""
        if context.job_type != JobType.SKU_MAPPING.value:
            raise JobExecutionError("invalid_job_type", "The SKU-mapping job type is invalid.")
        run_id = uuid.uuid5(context.processing_job_id, str(context.attempt_number))
        runtime = None
        try:
            async with self._sessions() as session:
                job = await session.scalar(select(ProcessingJob).where(
                    ProcessingJob.id == context.processing_job_id,
                    ProcessingJob.organization_id == context.organization_id,
                    ProcessingJob.document_id == context.document_id,
                ))
                if job is None or job.analysis_run_id is None or job.document_product_candidate_id is None:
                    raise JobExecutionError("sku_mapping_source_unavailable", "The SKU-mapping source is unavailable.")
                candidate = await session.scalar(select(DocumentProductCandidate).where(
                    DocumentProductCandidate.id == job.document_product_candidate_id,
                    DocumentProductCandidate.organization_id == context.organization_id,
                    DocumentProductCandidate.analysis_run_id == job.analysis_run_id,
                ))
                if candidate is None:
                    raise JobExecutionError("sku_mapping_candidate_unavailable", "The SKU-mapping candidate is unavailable.")
                analysis_run_id, external_candidate_id = job.analysis_run_id, candidate.external_candidate_id

            result = await self._analysis_results.load_completed_result(
                organization_id=context.organization_id, analysis_run_id=analysis_run_id,
            )
            try:
                task = build_sku_mapping_task(
                    result=result, analysis_run_id=analysis_run_id, candidate_id=external_candidate_id,
                    schema_version=SKU_MAPPING_TASK_SCHEMA_VERSION,
                )
            except SkuMappingTaskConstructionError as exc:
                raise JobExecutionError(exc.code, exc.safe_message) from None

            await self._mapping_results.create_run(
                run_id=run_id, organization_id=context.organization_id, document_id=context.document_id,
                analysis_run_id=analysis_run_id, candidate_id=external_candidate_id,
                attempt_number=context.attempt_number, schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION,
                document_analysis_schema_version=result.schema_version, prompt_version=SKU_MAPPING_PROMPT_VERSION,
                skill_version=SKU_MAPPING_SKILL_VERSION, agent_version=SKU_MAPPING_AGENT_VERSION,
                retriever_version=HYBRID_SKU_RETRIEVER_VERSION, model=self._settings.sku_mapping_model,
                started_at=datetime.now(UTC), processing_job_id=context.processing_job_id,
            )

            repository = PostgresSkuRepository(self._sessions)
            retriever = DeterministicHybridSkuRetriever(repository)
            document_tools = PersistedDocumentTools(DocumentAnalysisRepository(self._sessions), self._storage)
            tools = PersistedSkuMappingTools(retriever, repository, document_tools, task)
            execution = await self._agent.execute(task, tools)
            runtime = execution.runtime
            logger.info(
                "sku_mapping_runtime",
                outcome=execution.decision.outcome.value,
                tool_calls_by_name=runtime.tool_calls_by_name,
                correction_attempt_count=runtime.correction_attempt_count,
                terminal_reason=runtime.terminal_reason,
            )
            if runtime.last_retrieval_result is None:
                raise JobExecutionError("sku_mapping_retrieval_missing", "SKU-mapping retrieval result is unavailable.")
            artifact = SkuMappingRunArtifact(
                schema_version=SKU_MAPPING_ARTIFACT_SCHEMA_VERSION, task=task,
                retrieval=runtime.last_retrieval_result, decision=execution.decision,
            )
            await self._mapping_results.save_completed_result(run_id=run_id, artifact=artifact, runtime=runtime)
            loaded = await self._mapping_results.load_completed_result(organization_id=context.organization_id, run_id=run_id)
            if loaded != artifact:
                raise JobExecutionError("sku_mapping_reload_failed", "SKU-mapping persistence validation failed.")
            await schedule_normalization_if_ready(self._sessions, organization_id=context.organization_id, document_id=context.document_id, analysis_run_id=analysis_run_id, completed_job_id=context.processing_job_id)
        except JobExecutionError as exc:
            await self._fail(run_id, exc.code, exc.safe_message, runtime)
            raise
        except SkuMappingError as exc:
            await self._fail(run_id, exc.code, exc.safe_message, runtime or getattr(exc, "runtime", None))
            raise JobExecutionError(exc.code, exc.safe_message) from None
        except Exception as exc:
            await self._fail(run_id, "sku_mapping_failed", "SKU mapping failed.", runtime)
            raise JobExecutionError("sku_mapping_failed", "SKU mapping failed.") from exc

    async def _fail(self, run_id: uuid.UUID, code: str, message: str, runtime=None) -> None:
        """Best-effort record a safe mapping-run failure without changing other statuses."""
        try:
            await self._mapping_results.mark_run_failed(run_id, error_code=code, error_message=message, runtime=runtime)
        except SkuMappingError:
            pass
