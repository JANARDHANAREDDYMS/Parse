"""Database-backed tests for `NormalizationHandler`.

No supplied PDFs, no paid Claude call -- the bounded semantic-review agent
is replaced by an in-memory stub that records what it was asked to review
and returns a fixed, generated finding. Everything else runs through the
real persistence services, exactly as the worker runner would invoke it.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from maximor.config import get_database_settings
from maximor.db.models import NormalizationRun, NormalizationSemanticFindingProjection, ProcessingJob
from maximor.db.session import get_session_factory
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.jobs.errors import JobExecutionError
from maximor.jobs.types import JobContext, JobType
from maximor.normalization.persistence import NormalizationPersistenceService
from maximor.normalization.schemas import NormalizationRunStatus
from maximor.normalization.semantic_review.errors import SemanticReviewRuntimeError
from maximor.normalization.semantic_review.schemas import (
    NormalizationSemanticReviewResult,
    SemanticReviewFinding,
    SemanticReviewOutcome,
    SemanticReviewOwner,
)
from maximor.sku_mapping.persistence import SkuMappingPersistenceService
from maximor.storage import LocalObjectStorage
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
from maximor.term_applicability.schemas import (
    CandidateCommercialFacts,
    RawCommercialFact,
    RawCommercialFactField,
    TermApplicabilityResult,
    compute_fact_id,
)
from maximor.term_triage.schemas import TermTriageResult
from maximor.worker.handlers.normalization import NormalizationHandler
from test_normalization_repository import _seed_completed_sku_mapping_run
from test_sku_mapping_worker_integration import _seed


class _StubSemanticAgent:
    """Record the one task it was given and return one fixed finding."""

    def __init__(self, finding_outcome: SemanticReviewOutcome = SemanticReviewOutcome.ROUTE_FOR_TARGETED_CORRECTION):
        self.calls: list = []
        self._finding_outcome = finding_outcome

    async def review(self, task, tools) -> NormalizationSemanticReviewResult:
        self.calls.append(task)
        item = task.items[0]
        return NormalizationSemanticReviewResult(
            schema_version="1.0.0", organization_id=task.request.organization_id, document_id=task.request.document_id,
            preprocessing_run_id=task.request.preprocessing_run_id, analysis_run_id=task.request.analysis_run_id,
            normalization_schema_version=task.request.normalization_schema_version,
            finalization_policy_version=task.request.finalization_policy_version,
            prompt_version=task.request.prompt_version, skill_version=task.request.skill_version,
            agent_version=task.request.agent_version,
            findings=(SemanticReviewFinding(
                review_item_id=item.review_item_id, outcome=self._finding_outcome,
                owner=SemanticReviewOwner.TERM_APPLICABILITY, candidate_id=item.candidate_id,
                field_name=item.field_name, rationale="stub finding for testing",
            ),),
        )


class _EvidenceRetrievingStubAgent:
    """Actually exercise the wired tools/resolver before returning one finding.

    Unlike `_StubSemanticAgent`, this calls `get_review_item_context` and
    `get_review_item_evidence` for real -- proving the resolver handed to
    `NormalizationSemanticReviewTools` by the handler is a working one, not
    just present. Records the resolved evidence region for assertion.
    """

    def __init__(self) -> None:
        self.resolved_evidence: list = []

    async def review(self, task, tools) -> NormalizationSemanticReviewResult:
        item = task.items[0]
        context = await tools.get_review_item_context(item.review_item_id)
        assert context.review_item_id == item.review_item_id
        evidence_id = item.allowed_evidence_ids[0]
        region = await tools.get_review_item_evidence(item.review_item_id, evidence_id)
        self.resolved_evidence.append(region)
        return NormalizationSemanticReviewResult(
            schema_version="1.0.0", organization_id=task.request.organization_id, document_id=task.request.document_id,
            preprocessing_run_id=task.request.preprocessing_run_id, analysis_run_id=task.request.analysis_run_id,
            normalization_schema_version=task.request.normalization_schema_version,
            finalization_policy_version=task.request.finalization_policy_version,
            prompt_version=task.request.prompt_version, skill_version=task.request.skill_version,
            agent_version=task.request.agent_version,
            findings=(SemanticReviewFinding(
                review_item_id=item.review_item_id, outcome=SemanticReviewOutcome.INSUFFICIENT_EVIDENCE,
                candidate_id=item.candidate_id, field_name=item.field_name, rationale="resolver exercised for testing",
            ),),
        )


class _FakeSemanticRuntime:
    """Minimal stand-in for `SemanticReviewRuntimeSummary.safe_summary()`."""

    def safe_summary(self) -> dict:
        return {"failure_stage": "timeout", "tool_call_count": 2}


class _TimingOutSemanticAgent:
    """Simulate a genuine semantic-review subsystem failure with bounded diagnostics."""

    async def review(self, task, tools):
        raise SemanticReviewRuntimeError("normalization_semantic_review_timeout", runtime=_FakeSemanticRuntime())


class _RaisingSemanticAgent:
    """Fail the test loudly if semantic review is invoked when it should be skipped."""

    async def review(self, task, tools):
        raise AssertionError("semantic review must not be invoked for clean deterministic output")


async def _empty_term_applicability_run(ids: dict, storage: LocalObjectStorage, organization_id: uuid.UUID) -> uuid.UUID:
    service = TermApplicabilityPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization_id, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1, schema_version="v",
        prompt_version="p", skill_version="s", agent_version="a", model="fake", started_at=datetime.now(UTC),
    )
    triage = TermTriageResult(
        schema_version="v", organization_id=organization_id, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"], decisions=(),
    )
    applicability = TermApplicabilityResult(
        schema_version="v", organization_id=organization_id, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        decisions=(), candidate_commercial_facts=(), candidate_commercial_fact_coverage=(),
    )
    await service.save_completed_result(run_id=run_id, triage=triage, applicability=applicability)
    return run_id


async def _conflicting_term_applicability_run(ids: dict, storage: LocalObjectStorage, organization_id: uuid.UUID) -> uuid.UUID:
    """Persist facts that deterministically produce one non-hard `conflicting_values` issue.

    `candidate-purchased` has no raw-attribute hints (see `_seed`), so no
    `CandidateCommercialFactCoverage` declaration is required here.
    """

    service = TermApplicabilityPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization_id, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1, schema_version="v",
        prompt_version="p", skill_version="s", agent_version="a", model="fake", started_at=datetime.now(UTC),
    )
    triage = TermTriageResult(
        schema_version="v", organization_id=organization_id, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"], decisions=(),
    )
    evidence = ()
    from maximor.document_analysis.schemas import EvidenceReference, EvidenceRepresentation, ExtractionSource
    evidence = (EvidenceReference(
        preprocessing_run_id=ids["preprocessing_run_id"], page_number=1, block_id="native:p0001:b000000",
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    ),)

    def _fact(field: RawCommercialFactField, raw_value: str) -> RawCommercialFact:
        return RawCommercialFact(
            fact_id=compute_fact_id(candidate_id="candidate-purchased", field=field, raw_value=raw_value),
            candidate_id="candidate-purchased", field=field, raw_value=raw_value, evidence=evidence,
        )

    facts = (
        CandidateCommercialFacts(candidate_id="candidate-purchased", facts=(
            _fact(RawCommercialFactField.QUANTITY, "2"),
            _fact(RawCommercialFactField.UNIT_PRICE, "USD 10.00"),
            _fact(RawCommercialFactField.TOTAL_LISTED_VALUE, "USD 25.00"),
        )),
    )
    applicability = TermApplicabilityResult(
        schema_version="v", organization_id=organization_id, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        decisions=(), candidate_commercial_facts=facts, candidate_commercial_fact_coverage=(),
    )
    await service.save_completed_result(run_id=run_id, triage=triage, applicability=applicability)
    return run_id


async def _ambiguous_unit_price_term_applicability_run(ids: dict, storage: LocalObjectStorage, organization_id: uuid.UUID) -> uuid.UUID:
    """Persist one fact whose raw value is genuinely ambiguous, not merely arithmetic.

    `unit_price` raw text naming two numbers ("USD 10.00 or USD 12.00") makes
    `normalize_money` raise `ambiguous_money` -- a value the deterministic
    normalizer could not resolve at all, as opposed to
    `_conflicting_term_applicability_run`'s two *successfully* normalized
    numbers that merely disagree arithmetically. This is the genuinely
    eligible semantic-review case: `ambiguous_money` classifies as
    `AMBIGUOUS_VALUE`, which the eligibility set still (correctly) allows
    into semantic review.
    """

    service = TermApplicabilityPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization_id, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1, schema_version="v",
        prompt_version="p", skill_version="s", agent_version="a", model="fake", started_at=datetime.now(UTC),
    )
    triage = TermTriageResult(
        schema_version="v", organization_id=organization_id, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"], decisions=(),
    )
    from maximor.document_analysis.schemas import EvidenceReference, EvidenceRepresentation, ExtractionSource
    evidence = (EvidenceReference(
        preprocessing_run_id=ids["preprocessing_run_id"], page_number=1, block_id="native:p0001:b000000",
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    ),)
    fact = RawCommercialFact(
        fact_id=compute_fact_id(candidate_id="candidate-purchased", field=RawCommercialFactField.UNIT_PRICE, raw_value="USD 10.00 or USD 12.00"),
        candidate_id="candidate-purchased", field=RawCommercialFactField.UNIT_PRICE, raw_value="USD 10.00 or USD 12.00", evidence=evidence,
    )
    facts = (CandidateCommercialFacts(candidate_id="candidate-purchased", facts=(fact,)),)
    applicability = TermApplicabilityResult(
        schema_version="v", organization_id=organization_id, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        decisions=(), candidate_commercial_facts=facts, candidate_commercial_fact_coverage=(),
    )
    await service.save_completed_result(run_id=run_id, triage=triage, applicability=applicability)
    return run_id


def _normalization_job(organization_id: uuid.UUID, ids: dict) -> ProcessingJob:
    return ProcessingJob(
        id=uuid.uuid4(), organization_id=organization_id, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], job_type=JobType.NORMALIZATION.value,
        status="running", attempt_number=1,
    )


async def _persist_job(job: ProcessingJob) -> None:
    async with get_session_factory()() as session:
        async with session.begin():
            session.add(job)


def _handler(storage: LocalObjectStorage, semantic_agent) -> NormalizationHandler:
    sessions = get_session_factory()
    return NormalizationHandler(
        get_database_settings(), sessions, storage,
        DocumentAnalysisPersistenceService(sessions, storage),
        TermApplicabilityPersistenceService(sessions, storage),
        SkuMappingPersistenceService(sessions, storage),
        NormalizationPersistenceService(sessions, storage),
        semantic_agent=semantic_agent,
    )


@pytest.mark.asyncio
async def test_completed_outcome_for_clean_deterministic_output_skips_semantic_review(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _seed_completed_sku_mapping_run(ids, storage, organization)
    await _empty_term_applicability_run(ids, storage, organization)
    job = _normalization_job(organization, ids)
    await _persist_job(job)

    agent = _RaisingSemanticAgent()
    handler = _handler(storage, agent)
    context = JobContext(organization_id=organization, document_id=ids["document_id"], processing_job_id=job.id, job_type=JobType.NORMALIZATION.value, attempt_number=1)
    await handler.execute(context)

    async with get_session_factory()() as session:
        run = await session.scalar(select(NormalizationRun).where(NormalizationRun.processing_job_id == job.id))
        refreshed_job = await session.get(ProcessingJob, job.id)
    assert run.status == "completed"
    assert refreshed_job.status == "running"  # the handler never touches processing-job status


@pytest.mark.asyncio
async def test_review_required_outcome_invokes_semantic_review_without_mutating_normalized_values(tmp_path, organization):
    """A genuinely ambiguous raw value (not an arithmetic mismatch) is eligible and reviewed."""
    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _seed_completed_sku_mapping_run(ids, storage, organization)
    await _ambiguous_unit_price_term_applicability_run(ids, storage, organization)
    job = _normalization_job(organization, ids)
    await _persist_job(job)

    agent = _StubSemanticAgent()
    handler = _handler(storage, agent)
    context = JobContext(organization_id=organization, document_id=ids["document_id"], processing_job_id=job.id, job_type=JobType.NORMALIZATION.value, attempt_number=1)
    await handler.execute(context)

    assert len(agent.calls) == 1  # invoked exactly once

    async with get_session_factory()() as session:
        run = await session.scalar(select(NormalizationRun).where(NormalizationRun.processing_job_id == job.id))
        refreshed_job = await session.get(ProcessingJob, job.id)
        findings = (await session.scalars(select(NormalizationSemanticFindingProjection).where(NormalizationSemanticFindingProjection.normalization_run_id == run.id))).all()
    assert run.status == "review_required"
    assert refreshed_job.status == "running"
    assert len(findings) == 1
    assert findings[0].outcome == SemanticReviewOutcome.ROUTE_FOR_TARGETED_CORRECTION.value

    normalization_results = NormalizationPersistenceService(get_session_factory(), storage)
    loaded = await normalization_results.load_completed_result(organization_id=organization, run_id=run.id)
    line = loaded.extraction.line_items[0]
    # The deterministic outcome (unit_price left unresolved, since the raw
    # value was genuinely ambiguous) is preserved exactly as computed --
    # semantic review recommended a correction route, it did not silently
    # apply one or invent a resolved price.
    assert line.unit_price is None
    assert loaded.semantic_findings[0].outcome == SemanticReviewOutcome.ROUTE_FOR_TARGETED_CORRECTION.value


@pytest.mark.asyncio
async def test_pure_arithmetic_conflict_skips_semantic_review_and_persists_review_required_directly(tmp_path, organization):
    """A purely arithmetic/reconciliation mismatch must never reach the paid semantic-review agent.

    Regression for the exact live-pipeline defect this session diagnosed:
    `conflicting_values`/`line_total_conflict` are both deterministic
    arithmetic reconciliation differences (quantity*unit_price disagreeing
    with a supplied total), not a genuine semantic ambiguity -- Stage 5A
    must classify them as `RECONCILIATION_DIFFERENCE`, which
    `ELIGIBLE_REVIEW_CLASSIFICATIONS` never sends to semantic review.
    """
    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _seed_completed_sku_mapping_run(ids, storage, organization)
    await _conflicting_term_applicability_run(ids, storage, organization)
    job = _normalization_job(organization, ids)
    await _persist_job(job)

    agent = _RaisingSemanticAgent()
    handler = _handler(storage, agent)
    context = JobContext(organization_id=organization, document_id=ids["document_id"], processing_job_id=job.id, job_type=JobType.NORMALIZATION.value, attempt_number=1)
    await handler.execute(context)  # would raise via _RaisingSemanticAgent if reached

    async with get_session_factory()() as session:
        run = await session.scalar(select(NormalizationRun).where(NormalizationRun.processing_job_id == job.id))
        refreshed_job = await session.get(ProcessingJob, job.id)
        findings = (await session.scalars(select(NormalizationSemanticFindingProjection).where(NormalizationSemanticFindingProjection.normalization_run_id == run.id))).all()
    assert run.status == "review_required"
    assert refreshed_job.status == "running"
    assert findings == []

    normalization_results = NormalizationPersistenceService(get_session_factory(), storage)
    loaded = await normalization_results.load_completed_result(organization_id=organization, run_id=run.id)
    assert loaded.semantic_findings == ()
    codes = {issue.code for issue in loaded.finalization_issues}
    assert "conflicting_values" in codes
    classifications = {issue.classification.value for issue in loaded.finalization_issues}
    assert classifications == {"reconciliation_difference"}


@pytest.mark.asyncio
async def test_semantic_review_tools_receive_a_working_evidence_resolver(tmp_path, organization):
    """The handler wires a resolver that actually resolves persisted evidence, not `None`."""
    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _seed_completed_sku_mapping_run(ids, storage, organization)
    await _ambiguous_unit_price_term_applicability_run(ids, storage, organization)
    job = _normalization_job(organization, ids)
    await _persist_job(job)

    agent = _EvidenceRetrievingStubAgent()
    handler = _handler(storage, agent)
    context = JobContext(organization_id=organization, document_id=ids["document_id"], processing_job_id=job.id, job_type=JobType.NORMALIZATION.value, attempt_number=1)
    await handler.execute(context)

    assert len(agent.resolved_evidence) == 1
    resolved = agent.resolved_evidence[0]
    assert resolved is not None
    assert resolved.block_id == "native:p0001:b000000"

    async with get_session_factory()() as session:
        run = await session.scalar(select(NormalizationRun).where(NormalizationRun.processing_job_id == job.id))
    assert run.status == "review_required"


@pytest.mark.asyncio
async def test_semantic_review_typed_error_preserves_code_stage_and_runtime_diagnostics(tmp_path, organization):
    """A genuine semantic-review subsystem failure keeps its own code/stage/diagnostics.

    Regression for the exact live-pipeline defect this session diagnosed:
    the handler used to collapse every semantic-review failure into a
    generic `normalization_failed`/`execution` pair with no
    `runtime_diagnostics`, making it impossible to tell what actually went
    wrong without a fresh (paid) reproduction.
    """
    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _seed_completed_sku_mapping_run(ids, storage, organization)
    await _ambiguous_unit_price_term_applicability_run(ids, storage, organization)
    job = _normalization_job(organization, ids)
    await _persist_job(job)

    agent = _TimingOutSemanticAgent()
    handler = _handler(storage, agent)
    context = JobContext(organization_id=organization, document_id=ids["document_id"], processing_job_id=job.id, job_type=JobType.NORMALIZATION.value, attempt_number=1)

    with pytest.raises(JobExecutionError) as excinfo:
        await handler.execute(context)
    assert excinfo.value.code == "normalization_semantic_review_timeout"

    async with get_session_factory()() as session:
        run = await session.scalar(select(NormalizationRun).where(NormalizationRun.processing_job_id == job.id))
    assert run.status == "failed"
    assert run.error_code == "normalization_semantic_review_timeout"
    assert run.error_stage == "semantic_review"
    assert run.runtime_diagnostics == {"failure_stage": "timeout", "tool_call_count": 2}


@pytest.mark.asyncio
async def test_failed_validation_outcome_when_an_eligible_candidate_has_no_completed_sku_mapping(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    # Deliberately do not seed a completed sku_mapping run for candidate-purchased.
    await _empty_term_applicability_run(ids, storage, organization)
    job = _normalization_job(organization, ids)
    await _persist_job(job)

    agent = _RaisingSemanticAgent()
    handler = _handler(storage, agent)
    context = JobContext(organization_id=organization, document_id=ids["document_id"], processing_job_id=job.id, job_type=JobType.NORMALIZATION.value, attempt_number=1)

    # This must complete the processing job successfully -- a missing
    # completed dependency is a durable business outcome, not an
    # infrastructure failure.
    await handler.execute(context)

    async with get_session_factory()() as session:
        run = await session.scalar(select(NormalizationRun).where(NormalizationRun.processing_job_id == job.id))
        refreshed_job = await session.get(ProcessingJob, job.id)
    assert run.status == "failed_validation"
    assert refreshed_job.status == "running"

    normalization_results = NormalizationPersistenceService(get_session_factory(), storage)
    loaded = await normalization_results.load_completed_result(organization_id=organization, run_id=run.id)
    assert loaded.status is NormalizationRunStatus.FAILED_VALIDATION
    assert loaded.finalization_issues[0].code == "normalization_sku_mapping_missing"
    assert loaded.finalization_issues[0].hard is True
