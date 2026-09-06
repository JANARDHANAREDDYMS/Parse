"""Test SKU-mapping worker integration: schema constraints, eligibility-driven
scheduling, concurrent-claim protection, retry, and SkuMappingHandler persistence
behavior. No live Claude call anywhere in this file — SkuMappingHandler tests
inject a fake agent.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from maximor.db.models import (
    CatalogVersion,
    Document,
    DocumentAnalysisRun,
    DocumentBlock,
    DocumentCommercialStatusAssessment,
    DocumentPage,
    DocumentProcessingRun,
    DocumentProductCandidate,
    ProcessingJob,
    Sku,
)
from maximor.config import get_database_settings
from maximor.db.models.statuses import CatalogStatus
from maximor.db.session import get_session_factory
from maximor.document_analysis.agent import DocumentAnalysisRuntimeSummary
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.document_analysis.schemas import (
    CommercialStatus,
    CommercialStatusAssessment,
    DocumentAnalysisResult,
    EvidenceReference,
    EvidenceRepresentation,
    ExtractionSource,
    ProductCandidate,
)
from maximor.jobs.errors import JobExecutionError
from maximor.jobs.service import schedule_sku_mapping_job
from maximor.jobs.types import JobContext, JobType
from maximor.preprocessing.schemas import BoundingBox
from maximor.sku_mapping.agent import SkuMappingExecution, SkuMappingRuntimeSummary
from maximor.sku_mapping.contracts import SkuMappingTask, build_sku_mapping_task
from maximor.sku_mapping.errors import SkuMappingError
from maximor.sku_mapping.persistence import SkuMappingPersistenceService
from maximor.sku_mapping.schemas import (
    RetrievedSku,
    SkuMappingDecision,
    SkuMappingOutcome,
    SkuMatchSource,
    SkuRecord,
    SkuRetrievalResult,
)
from maximor.sku_mapping.versions import HYBRID_SKU_RETRIEVER_VERSION, SKU_MAPPING_DECISION_SCHEMA_VERSION, SKU_RETRIEVAL_SCHEMA_VERSION
from maximor.storage import LocalObjectStorage
from maximor.worker.handlers.document_analysis import DocumentAnalysisHandler
from maximor.worker.handlers.sku_mapping import SkuMappingHandler


async def _seed(organization_id: uuid.UUID, storage: LocalObjectStorage, *, with_catalog: bool = True) -> dict:
    """Create a completed preprocessing+analysis run with 3 real candidates and one SKU.

    Candidates are `purchased` (SCHEDULE), `optional` (SKIP), and `ambiguous`
    (REVIEW) — covering every disposition the eligibility policy produces.
    The analysis result is persisted for real (not a fake storage key), so a
    `SkuMappingHandler` under test can actually `load_completed_result` it.

    `with_catalog=False` skips the CatalogVersion/Sku rows — an organization
    may have only one *active* catalog version at a time, so a test seeding a
    second, unrelated run under the same organization (to exercise
    cross-lineage checks, not retrieval) must skip this.
    """

    document_id, job_id, preprocessing_run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    analysis_run_id, catalog_version_id, sku_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    checksum = uuid.uuid4().hex + uuid.uuid4().hex
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        async with session.begin():
            session.add(Document(id=document_id, organization_id=organization_id, original_filename="generated.pdf", storage_key=f"generated/{document_id}.pdf", sha256_checksum=checksum, media_type="application/pdf", status="completed"))
            session.add(ProcessingJob(id=job_id, organization_id=organization_id, document_id=document_id, job_type="document_preprocessing", status="completed", attempt_number=1))
            await session.flush()
            session.add(DocumentProcessingRun(id=preprocessing_run_id, organization_id=organization_id, document_id=document_id, processing_job_id=job_id, attempt_number=1, status="completed", schema_version="prep-v1", processor_version="test", original_document_checksum=checksum, page_count=1, started_at=now, completed_at=now))
            await session.flush()
            page = DocumentPage(organization_id=organization_id, processing_run_id=preprocessing_run_id, page_number=1, width_points=100, height_points=100, rotation_degrees=0, native_plain_text="generated", native_character_count=9, native_word_count=1, ocr_status="not_required", ocr_plain_text=None, quality={}, render_storage_key="generated/render.png", render_media_type="image/png", render_pixel_width=1, render_pixel_height=1, render_dpi=72, render_checksum="b" * 64, warnings=[], created_at=now)
            session.add(page)
            await session.flush()
            session.add(DocumentBlock(id=uuid.uuid4(), organization_id=organization_id, processing_run_id=preprocessing_run_id, document_page_id=page.id, external_block_id="native:p0001:b000000", representation="native_text", extraction_source="native", reading_order=0, text="generated", x0=1, y0=2, x1=30, y1=12, created_at=now))
            if with_catalog:
                session.add(CatalogVersion(id=catalog_version_id, organization_id=organization_id, version_identifier="v1", status=CatalogStatus.ACTIVE.value))
                await session.flush()
                session.add(Sku(id=sku_id, organization_id=organization_id, catalog_version_id=catalog_version_id, source_sku_id=uuid.uuid4(), sku_code="PREMIUM_SUPPORT", name="Premium Support", aliases=[], source_attributes={}, is_active=True))

    def _evidence() -> EvidenceReference:
        return EvidenceReference(
            preprocessing_run_id=preprocessing_run_id, page_number=1, block_id="native:p0001:b000000",
            representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
            bounding_box=BoundingBox(x0=1, y0=2, x1=30, y1=12),
        )

    candidates = (
        ProductCandidate(candidate_id="candidate-ambiguous", raw_name="Ambiguous Thing", evidence=(_evidence(),)),
        ProductCandidate(candidate_id="candidate-optional", raw_name="Optional Thing", evidence=(_evidence(),)),
        ProductCandidate(candidate_id="candidate-purchased", raw_name="Premium Support", evidence=(_evidence(),)),
    )
    statuses = (
        CommercialStatusAssessment(assessment_id="status-ambiguous", status=CommercialStatus.AMBIGUOUS, candidate_id="candidate-ambiguous", evidence=(_evidence(),)),
        CommercialStatusAssessment(assessment_id="status-optional", status=CommercialStatus.OPTIONAL, candidate_id="candidate-optional", evidence=(_evidence(),)),
        CommercialStatusAssessment(assessment_id="status-purchased", status=CommercialStatus.PURCHASED, candidate_id="candidate-purchased", evidence=(_evidence(),)),
    )
    result = DocumentAnalysisResult(
        schema_version="analysis-v1", organization_id=organization_id, document_id=document_id,
        preprocessing_run_id=preprocessing_run_id, preprocessing_schema_version="prep-v1",
        prompt_version="p1", agent_version="a1", product_candidates=candidates, commercial_statuses=statuses,
    )
    analysis_persistence = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    await analysis_persistence.create_run(
        run_id=analysis_run_id, organization_id=organization_id, document_id=document_id,
        preprocessing_run_id=preprocessing_run_id, attempt_number=1, schema_version="analysis-v1",
        preprocessing_schema_version="prep-v1", prompt_version="p1", skill_version="s1", agent_version="a1",
        model="test-model", started_at=now,
    )
    analysis_runtime = DocumentAnalysisRuntimeSummary(
        request_id=uuid.uuid4(), started_at=now, tool_call_count=2,
        tool_calls_by_name={"get_document_overview": 1, "search_document": 1},
    )
    await analysis_persistence.save_completed_result(analysis_run_id=analysis_run_id, result=result, runtime=analysis_runtime)

    async with get_session_factory()() as session:
        rows = (await session.scalars(
            select(DocumentProductCandidate).where(DocumentProductCandidate.analysis_run_id == analysis_run_id)
        )).all()
    candidate_ids = {row.external_candidate_id: row.id for row in rows}
    return dict(
        document_id=document_id, preprocessing_run_id=preprocessing_run_id, analysis_run_id=analysis_run_id,
        candidate_ids=candidate_ids, catalog_version_id=catalog_version_id, sku_id=sku_id,
    )


# --- Schema constraint tests -------------------------------------------------

@pytest.mark.asyncio
async def test_sku_mapping_link_required_check_rejects_partial_fields(tmp_path, organization):
    """Reject a sku_mapping job missing one of its two required linking fields."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    candidate_id = ids["candidate_ids"]["candidate-purchased"]
    async with get_session_factory()() as session:
        session.add(ProcessingJob(
            id=uuid.uuid4(), organization_id=organization, document_id=ids["document_id"],
            job_type="sku_mapping", status="queued", attempt_number=1,
            analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=None,
        ))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_sku_mapping_link_required_check_rejects_fields_on_other_job_types(tmp_path, organization):
    """Reject a non-sku_mapping job that sets the sku_mapping-only linking fields."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    candidate_id = ids["candidate_ids"]["candidate-purchased"]
    async with get_session_factory()() as session:
        session.add(ProcessingJob(
            id=uuid.uuid4(), organization_id=organization, document_id=ids["document_id"],
            job_type="document_analysis", status="queued", attempt_number=1,
            analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=candidate_id,
        ))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_composite_fk_rejects_candidate_from_a_different_analysis_run(tmp_path, organization):
    """Reject a job whose candidate does not actually belong to its claimed analysis run."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    other_ids = await _seed(organization, LocalObjectStorage(tmp_path), with_catalog=False)
    mismatched_candidate_id = other_ids["candidate_ids"]["candidate-purchased"]
    async with get_session_factory()() as session:
        session.add(ProcessingJob(
            id=uuid.uuid4(), organization_id=organization, document_id=ids["document_id"],
            job_type="sku_mapping", status="queued", attempt_number=1,
            analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=mismatched_candidate_id,
        ))
        with pytest.raises(IntegrityError):
            await session.commit()


# --- schedule_sku_mapping_job -------------------------------------------------

@pytest.mark.asyncio
async def test_schedule_sku_mapping_job_is_idempotent(tmp_path, organization):
    """Calling schedule twice for the same candidate returns the same job."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    candidate_id = ids["candidate_ids"]["candidate-purchased"]
    first = await schedule_sku_mapping_job(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=candidate_id,
    )
    second = await schedule_sku_mapping_job(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=candidate_id,
    )
    assert first.id == second.id and first.attempt_number == 1


@pytest.mark.asyncio
async def test_schedule_sku_mapping_job_retries_after_terminal_status(tmp_path, organization):
    """A new sequential attempt is created once the prior job reaches a terminal status."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    candidate_id = ids["candidate_ids"]["candidate-purchased"]
    first = await schedule_sku_mapping_job(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=candidate_id,
    )
    async with get_session_factory()() as session:
        async with session.begin():
            job = await session.get(ProcessingJob, first.id)
            job.status = "failed"

    retried = await schedule_sku_mapping_job(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=candidate_id,
        retry_terminal=True,
    )
    assert retried.id != first.id and retried.attempt_number == 2


@pytest.mark.asyncio
async def test_concurrent_active_sku_mapping_jobs_for_same_candidate_are_rejected(tmp_path, organization):
    """The partial unique index blocks two concurrently active jobs for one candidate."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    candidate_id = ids["candidate_ids"]["candidate-purchased"]
    async with get_session_factory()() as session:
        async with session.begin():
            session.add(ProcessingJob(
                id=uuid.uuid4(), organization_id=organization, document_id=ids["document_id"],
                job_type="sku_mapping", status="queued", attempt_number=1,
                analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=candidate_id,
            ))
    async with get_session_factory()() as session:
        session.add(ProcessingJob(
            id=uuid.uuid4(), organization_id=organization, document_id=ids["document_id"],
            job_type="sku_mapping", status="queued", attempt_number=2,
            analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=candidate_id,
        ))
        with pytest.raises(IntegrityError):
            await session.commit()


# --- DocumentAnalysisHandler auto-scheduling ---------------------------------

@pytest.mark.asyncio
async def test_document_analysis_handler_schedules_only_eligible_candidates(tmp_path, organization):
    """Schedule sku_mapping only for the purchased candidate; skip optional; not ambiguous."""

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    handler = DocumentAnalysisHandler(get_database_settings(), get_session_factory(), storage, analysis_results)
    context = JobContext(organization_id=organization, document_id=ids["document_id"], processing_job_id=uuid.uuid4(), job_type="document_analysis", attempt_number=1)

    await handler._schedule_eligible_sku_mapping_jobs(context, ids["analysis_run_id"])

    async with get_session_factory()() as session:
        jobs = (await session.scalars(select(ProcessingJob).where(ProcessingJob.analysis_run_id == ids["analysis_run_id"]))).all()
    scheduled_candidate_ids = {job.document_product_candidate_id for job in jobs}
    assert scheduled_candidate_ids == {ids["candidate_ids"]["candidate-purchased"]}


# --- SkuMappingHandler (fakes only, no live Claude call) ---------------------

class _FakeSkuMappingAgent:
    """Return a fixed decision/execution without any Claude Agent SDK involvement."""

    def __init__(self, decision: SkuMappingDecision, retrieval: SkuRetrievalResult, runtime: SkuMappingRuntimeSummary) -> None:
        self._decision, self._retrieval, self._runtime = decision, retrieval, runtime

    async def execute(self, task: SkuMappingTask, tools) -> SkuMappingExecution:
        self._runtime.last_retrieval_result = self._retrieval
        return SkuMappingExecution(decision=self._decision, runtime=self._runtime)


def _fake_execution_for(task: SkuMappingTask, ids: dict, outcome: SkuMappingOutcome) -> _FakeSkuMappingAgent:
    retrieved = RetrievedSku(
        sku=SkuRecord(
            id=ids["sku_id"], source_sku_id=uuid.uuid4(), organization_id=task.organization_id,
            catalog_version_id=ids["catalog_version_id"], sku_code="PREMIUM_SUPPORT", name="Premium Support",
        ),
        score=1.0, matched_sources=(SkuMatchSource.EXACT,), matched_text="Premium Support",
    )
    retrieval = SkuRetrievalResult(
        schema_version=SKU_RETRIEVAL_SCHEMA_VERSION, retriever_version=HYBRID_SKU_RETRIEVER_VERSION,
        organization_id=task.organization_id, catalog_version_id=ids["catalog_version_id"],
        catalog_version_identifier="v1", candidate_id=task.candidate_id, candidates=(retrieved,),
    )
    if outcome is SkuMappingOutcome.MATCH:
        decision = SkuMappingDecision(
            schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION, organization_id=task.organization_id,
            document_id=task.document_id, preprocessing_run_id=task.preprocessing_run_id,
            analysis_run_id=task.analysis_run_id, candidate_id=task.candidate_id,
            catalog_version_id=ids["catalog_version_id"], outcome=SkuMappingOutcome.MATCH,
            sku_id=ids["sku_id"], sku_code="PREMIUM_SUPPORT", sku_name="Premium Support",
            evidence=task.candidate_evidence,
        )
    elif outcome is SkuMappingOutcome.AMBIGUOUS:
        decision = SkuMappingDecision(
            schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION, organization_id=task.organization_id,
            document_id=task.document_id, preprocessing_run_id=task.preprocessing_run_id,
            analysis_run_id=task.analysis_run_id, candidate_id=task.candidate_id,
            catalog_version_id=ids["catalog_version_id"], outcome=outcome,
            considered_sku_ids=(ids["sku_id"], uuid.uuid4()), evidence=task.candidate_evidence,
        )
    else:
        decision = SkuMappingDecision(
            schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION, organization_id=task.organization_id,
            document_id=task.document_id, preprocessing_run_id=task.preprocessing_run_id,
            analysis_run_id=task.analysis_run_id, candidate_id=task.candidate_id,
            catalog_version_id=ids["catalog_version_id"], outcome=outcome, evidence=task.candidate_evidence,
        )
    runtime = SkuMappingRuntimeSummary(
        request_id=uuid.uuid4(), started_at=datetime.now(UTC), tool_call_count=2,
        tool_calls_by_name={"retrieve_skus": 1, "get_authoritative_sku": 1} if outcome is SkuMappingOutcome.MATCH else {"retrieve_skus": 1},
        authoritative_skus_by_id={ids["sku_id"]: retrieved.sku} if outcome is SkuMappingOutcome.MATCH else {},
    )
    return _FakeSkuMappingAgent(decision, retrieval, runtime)


async def _run_handler(tmp_path, organization, ids, outcome: SkuMappingOutcome, storage=None):
    storage = storage or LocalObjectStorage(tmp_path)
    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    mapping_results = SkuMappingPersistenceService(get_session_factory(), storage)
    candidate_id = ids["candidate_ids"]["candidate-purchased"]
    job = await schedule_sku_mapping_job(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=candidate_id,
    )
    result = await analysis_results.load_completed_result(organization_id=organization, analysis_run_id=ids["analysis_run_id"])
    task = build_sku_mapping_task(result=result, analysis_run_id=ids["analysis_run_id"], candidate_id="candidate-purchased", schema_version="1.0.0")
    agent = _fake_execution_for(task, ids, outcome)
    handler = SkuMappingHandler(get_database_settings(), get_session_factory(), storage, analysis_results, mapping_results, agent=agent)
    context = JobContext(organization_id=organization, document_id=ids["document_id"], processing_job_id=job.id, job_type="sku_mapping", attempt_number=1)
    await handler.execute(context)
    return mapping_results, job


@pytest.mark.asyncio
async def test_sku_mapping_handler_completes_a_match_decision(tmp_path, organization):
    """A validated MATCH decision persists and reload-validates without raising."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    storage = LocalObjectStorage(tmp_path)
    mapping_results, job = await _run_handler(tmp_path, organization, ids, SkuMappingOutcome.MATCH, storage=storage)

    run_id = uuid.uuid5(job.id, "1")
    loaded = await mapping_results.load_completed_result(organization_id=organization, run_id=run_id)
    assert loaded.decision.outcome is SkuMappingOutcome.MATCH
    assert loaded.decision.sku_code == "PREMIUM_SUPPORT"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [SkuMappingOutcome.NO_MATCH, SkuMappingOutcome.AMBIGUOUS])
async def test_sku_mapping_handler_treats_no_match_and_ambiguous_as_completions(tmp_path, organization, outcome):
    """NO_MATCH and AMBIGUOUS are valid business outcomes, not technical failures."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    storage = LocalObjectStorage(tmp_path)

    mapping_results, job = await _run_handler(tmp_path, organization, ids, outcome, storage=storage)

    run_id = uuid.uuid5(job.id, "1")
    loaded = await mapping_results.load_completed_result(organization_id=organization, run_id=run_id)
    assert loaded.decision.outcome is outcome


@pytest.mark.asyncio
async def test_sku_mapping_handler_raises_on_missing_source(tmp_path, organization):
    """A job referencing a nonexistent analysis run/candidate raises JobExecutionError."""

    storage = LocalObjectStorage(tmp_path)
    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    mapping_results = SkuMappingPersistenceService(get_session_factory(), storage)
    handler = SkuMappingHandler(get_database_settings(), get_session_factory(), storage, analysis_results, mapping_results, agent=None)
    context = JobContext(organization_id=organization, document_id=uuid.uuid4(), processing_job_id=uuid.uuid4(), job_type="sku_mapping", attempt_number=1)

    with pytest.raises(JobExecutionError) as excinfo:
        await handler.execute(context)
    assert excinfo.value.code == "sku_mapping_source_unavailable"
