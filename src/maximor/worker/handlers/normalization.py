"""Run deterministic normalization and optional bounded semantic review."""
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from maximor.config import DatabaseSettings
from maximor.db.models import DocumentAnalysisRun, ProcessingJob, TermApplicabilityRun
from maximor.jobs.errors import JobExecutionError
from maximor.jobs.types import JobContext, JobType
from maximor.normalization.errors import NormalizationError
from maximor.normalization.finalization import build_finalization_candidate
from maximor.normalization.persistence import NormalizationPersistenceService
from maximor.normalization.repository import NormalizationRepository
from maximor.normalization.schemas import (
    FinalizationIssue,
    FinalOrderFormExtraction,
    NormalizationResult,
    NormalizationRunStatus,
    ReviewQueueClassification,
    ValidationStatus,
)
from maximor.normalization.semantic_review.agent import ClaudeNormalizationSemanticReviewAgent
from maximor.normalization.semantic_review.contracts import build_semantic_review_task
from maximor.normalization.semantic_review.errors import SemanticReviewTaskError
from maximor.normalization.semantic_review.tools import NormalizationSemanticReviewTools
from maximor.normalization.versions import FINALIZATION_POLICY_VERSION, NORMALIZATION_RESULT_SCHEMA_VERSION


def _dependency_issue(code: str, message: str) -> FinalizationIssue:
    """Build one hard finalization issue for a durable dependency failure.

    Used only when a required upstream branch (SKU mapping for an eligible
    candidate, or the term-applicability run itself) never reached a
    completed state -- a business-terminal condition this handler must
    still report honestly, not an infrastructure failure.
    """

    return FinalizationIssue(
        code=code, classification=ReviewQueueClassification.HARD_INVARIANT, message=message,
        location="dependencies", hard=True,
    )


class NormalizationHandler:
    """Own domain normalization status while leaving processing-job status to the runner."""

    def __init__(self, settings: DatabaseSettings, sessions, analysis_results, term_results, mapping_results, normalization_results: NormalizationPersistenceService, semantic_agent=None):
        self._settings = settings
        self._sessions = sessions
        self._normalization = normalization_results
        self._repository = NormalizationRepository(sessions, analysis_results, term_results, mapping_results)
        self._semantic = semantic_agent or ClaudeNormalizationSemanticReviewAgent(settings)

    async def execute(self, context: JobContext) -> None:
        """Build, optionally review, persist, and reload one normalization result."""
        if context.job_type != JobType.NORMALIZATION.value:
            raise JobExecutionError("invalid_job_type", "The normalization job type is invalid.")
        run_id = uuid.uuid5(context.processing_job_id, str(context.attempt_number))
        runtime = None
        try:
            async with self._sessions() as session:
                job = await session.scalar(select(ProcessingJob).where(ProcessingJob.id == context.processing_job_id, ProcessingJob.organization_id == context.organization_id, ProcessingJob.document_id == context.document_id))
                if job is None or job.analysis_run_id is None:
                    raise JobExecutionError("normalization_job_malformed", "The normalization job is missing its analysis-run link.")
                analysis_run = await session.scalar(select(DocumentAnalysisRun).where(DocumentAnalysisRun.id == job.analysis_run_id, DocumentAnalysisRun.organization_id == context.organization_id, DocumentAnalysisRun.document_id == context.document_id))
                # Deliberately not filtered by status: a term-applicability
                # run that technically failed still has a real id the fan-in
                # scheduler already saw before scheduling this job, and this
                # handler must be able to report that failure honestly
                # (FAILED_VALIDATION) rather than discard the lineage.
                term_run = await session.scalar(select(TermApplicabilityRun).where(TermApplicabilityRun.analysis_run_id == job.analysis_run_id, TermApplicabilityRun.organization_id == context.organization_id, TermApplicabilityRun.document_id == context.document_id).order_by(TermApplicabilityRun.attempt_number.desc()))
            if analysis_run is None or term_run is None:
                # The fan-in scheduler only ever schedules this job once both
                # rows exist; either being absent here is a genuine
                # infrastructure inconsistency, not an expected outcome.
                raise JobExecutionError("normalization_dependencies_unavailable", "Normalization dependencies are unavailable.")

            await self._normalization.create_run(
                run_id=run_id, organization_id=context.organization_id, document_id=context.document_id,
                preprocessing_run_id=analysis_run.preprocessing_run_id, analysis_run_id=job.analysis_run_id,
                term_applicability_run_id=term_run.id, attempt_number=context.attempt_number,
                processing_job_id=context.processing_job_id, schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION,
                finalization_policy_version=FINALIZATION_POLICY_VERSION, started_at=datetime.now(UTC),
            )

            dependency_issue = None
            if term_run.status != "completed":
                dependency_issue = _dependency_issue(
                    "normalization_term_applicability_run_not_completed",
                    "The term-applicability run did not reach a completed state.",
                )
            normalization_input = None
            if dependency_issue is None:
                try:
                    normalization_input = await self._repository.load_normalization_input(
                        organization_id=context.organization_id, document_id=context.document_id,
                        analysis_run_id=job.analysis_run_id, term_applicability_run_id=term_run.id,
                    )
                except NormalizationError as exc:
                    # A durable business dependency failure (an eligible
                    # candidate never got a completed SKU mapping or
                    # commercial-fact coverage, or the loaded sources
                    # disagree) -- persist an honest FAILED_VALIDATION
                    # domain result rather than failing the processing job.
                    dependency_issue = _dependency_issue(exc.code, exc.safe_message)

            if dependency_issue is not None:
                extraction = FinalOrderFormExtraction(
                    schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION, organization_id=context.organization_id,
                    document_id=context.document_id, preprocessing_run_id=analysis_run.preprocessing_run_id,
                    analysis_run_id=job.analysis_run_id, term_applicability_run_id=term_run.id,
                    validation_status=ValidationStatus.FAILED_VALIDATION,
                )
                result = NormalizationResult(
                    schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION, organization_id=context.organization_id,
                    document_id=context.document_id, preprocessing_run_id=analysis_run.preprocessing_run_id,
                    analysis_run_id=job.analysis_run_id, term_applicability_run_id=term_run.id,
                    finalization_policy_version=FINALIZATION_POLICY_VERSION, status=NormalizationRunStatus.FAILED_VALIDATION,
                    extraction=extraction, finalization_issues=(dependency_issue,),
                )
                await self._normalization.save_completed_result(run_id=run_id, result=result)
                loaded = await self._normalization.load_completed_result(organization_id=context.organization_id, run_id=run_id)
                if loaded != result:
                    raise JobExecutionError("normalization_reload_failed", "Normalization persistence validation failed.")
                return

            from maximor.normalization.service import assemble_normalized_draft
            draft = assemble_normalized_draft(normalization_input)
            candidate, readiness = build_finalization_candidate(normalization_input, draft)
            semantic_findings = ()
            if readiness.status.value == "review_required":
                try:
                    task = build_semantic_review_task(normalization_input, candidate, readiness)
                except SemanticReviewTaskError as exc:
                    if exc.code == "semantic_review_no_eligible_items":
                        task = None
                    else:
                        raise
                if task is not None:
                    review = await self._semantic.review(task, NormalizationSemanticReviewTools(task))
                    semantic_findings = tuple(
                        {"review_item_id": f.review_item_id, "outcome": f.outcome.value, "owner": f.owner.value if f.owner else None, "candidate_id": f.candidate_id, "field_name": f.field_name, "evidence_ids": f.evidence_ids, "rationale": f.rationale}
                        for f in review.findings
                    )
            domain_status = NormalizationRunStatus.COMPLETED if readiness.status.value == "ready_for_semantic_review" else NormalizationRunStatus.FAILED_VALIDATION if readiness.status.value == "failed_validation" else NormalizationRunStatus.REVIEW_REQUIRED
            if domain_status is NormalizationRunStatus.COMPLETED:
                candidate = candidate.model_copy(update={"validation_status": ValidationStatus.COMPLETED})
            result = NormalizationResult(schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION, organization_id=context.organization_id, document_id=context.document_id, preprocessing_run_id=normalization_input.preprocessing_run_id, analysis_run_id=normalization_input.analysis_run_id, term_applicability_run_id=normalization_input.term_applicability_run_id, finalization_policy_version=readiness.policy_version, status=domain_status, extraction=candidate, finalization_issues=readiness.issues, semantic_findings=semantic_findings)
            await self._normalization.save_completed_result(run_id=run_id, result=result)
            loaded = await self._normalization.load_completed_result(organization_id=context.organization_id, run_id=run_id)
            if loaded != result:
                raise JobExecutionError("normalization_reload_failed", "Normalization persistence validation failed.")
        except JobExecutionError:
            raise
        except Exception as exc:
            try:
                await self._normalization.mark_run_failed(run_id, error_code="normalization_failed", error_stage="execution")
            except Exception:
                pass
            raise JobExecutionError("normalization_failed", "Normalization could not be completed.") from exc
