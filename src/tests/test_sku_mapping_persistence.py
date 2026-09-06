"""Test SKU-mapping persistence using generated database projections and storage only."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from maximor.db.models import (
    CatalogVersion,
    Document,
    DocumentAnalysisRun,
    DocumentBlock,
    DocumentPage,
    DocumentProcessingRun,
    DocumentProductCandidate,
    ProcessingJob,
    Sku,
    SkuMappingDecisionProjection,
    SkuMappingEvidenceReference,
    SkuMappingRun,
)
from maximor.db.models.statuses import CatalogStatus
from maximor.db.session import get_session_factory
from maximor.document_analysis.schemas import CommercialStatus, EvidenceReference, EvidenceRepresentation, ExtractionSource
from maximor.preprocessing.schemas import BoundingBox
from maximor.sku_mapping.agent import SkuMappingRuntimeSummary
from maximor.sku_mapping.contracts import SkuMappingRunArtifact, SkuMappingTask
from maximor.sku_mapping.errors import SkuMappingPersistenceError
from maximor.sku_mapping.persistence import SkuMappingPersistenceService
from maximor.sku_mapping.schemas import (
    RetrievedSku,
    SkuMappingDecision,
    SkuMappingOutcome,
    SkuMatchSource,
    SkuRecord,
    SkuRetrievalResult,
)
from maximor.sku_mapping.versions import (
    HYBRID_SKU_RETRIEVER_VERSION,
    SKU_MAPPING_AGENT_VERSION,
    SKU_MAPPING_ARTIFACT_SCHEMA_VERSION,
    SKU_MAPPING_DECISION_SCHEMA_VERSION,
    SKU_MAPPING_PROMPT_VERSION,
    SKU_MAPPING_SKILL_VERSION,
    SKU_MAPPING_TASK_SCHEMA_VERSION,
    SKU_RETRIEVAL_SCHEMA_VERSION,
)
from maximor.storage import LocalObjectStorage

CANDIDATE_EXTERNAL_ID = "candidate-0001"


async def _source(organization_id: uuid.UUID, *, with_catalog: bool = True) -> dict:
    """Create a completed generated preprocessing+analysis run with one candidate.

    `with_catalog=False` skips creating a CatalogVersion/Sku — an
    organization may have only one *active* catalog version at a time, so a
    test creating a second unrelated `_source()` under the same organization
    (to exercise cross-lineage checks, not retrieval) must skip this.
    """

    document_id, job_id, preprocessing_run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    analysis_run_id, catalog_version_id, sku_id, candidate_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    document_checksum = (uuid.uuid4().hex + uuid.uuid4().hex)  # unique per call; documents enforce (org, checksum) uniqueness
    async with get_session_factory()() as session:
        async with session.begin():
            session.add(Document(id=document_id, organization_id=organization_id, original_filename="generated.pdf", storage_key=f"generated/{document_id}.pdf", sha256_checksum=document_checksum, media_type="application/pdf", status="completed"))
            session.add(ProcessingJob(id=job_id, organization_id=organization_id, document_id=document_id, job_type="document_preprocessing", status="completed", attempt_number=1))
            await session.flush()
            session.add(DocumentProcessingRun(id=preprocessing_run_id, organization_id=organization_id, document_id=document_id, processing_job_id=job_id, attempt_number=1, status="completed", schema_version="prep-v1", processor_version="test", original_document_checksum="a" * 64, page_count=1, started_at=now, completed_at=now))
            await session.flush()
            page = DocumentPage(organization_id=organization_id, processing_run_id=preprocessing_run_id, page_number=1, width_points=100, height_points=100, rotation_degrees=0, native_plain_text="generated", native_character_count=9, native_word_count=1, ocr_status="not_required", ocr_plain_text=None, quality={}, render_storage_key="generated/render.png", render_media_type="image/png", render_pixel_width=1, render_pixel_height=1, render_dpi=72, render_checksum="b" * 64, warnings=[], created_at=now)
            session.add(page)
            await session.flush()
            session.add(DocumentBlock(id=uuid.uuid4(), organization_id=organization_id, processing_run_id=preprocessing_run_id, document_page_id=page.id, external_block_id="native:p0001:b000000", representation="native_text", extraction_source="native", reading_order=0, text="generated", x0=1, y0=2, x1=30, y1=12, created_at=now))
            session.add(DocumentAnalysisRun(id=analysis_run_id, organization_id=organization_id, document_id=document_id, preprocessing_run_id=preprocessing_run_id, attempt_number=1, status="completed", schema_version="analysis-v1", preprocessing_schema_version="prep-v1", prompt_version="p1", skill_version="s1", agent_version="a1", model="test-model", started_at=now, completed_at=now, validation_status="validated", canonical_result_storage_key="fake-key", compression_method="gzip", compressed_sha256_checksum="c" * 64, content_sha256_checksum="d" * 64))
            await session.flush()
            session.add(DocumentProductCandidate(id=candidate_id, analysis_run_id=analysis_run_id, organization_id=organization_id, external_candidate_id=CANDIDATE_EXTERNAL_ID, source_order=0, raw_name="Premium Support", raw_attributes={}, evidence_count=1, created_at=now))
            if with_catalog:
                session.add(CatalogVersion(id=catalog_version_id, organization_id=organization_id, version_identifier="v1", status=CatalogStatus.ACTIVE.value))
                await session.flush()
                session.add(Sku(id=sku_id, organization_id=organization_id, catalog_version_id=catalog_version_id, source_sku_id=uuid.uuid4(), sku_code="PREMIUM_SUPPORT", name="Premium Support", aliases=[], source_attributes={}, is_active=True))
    return dict(
        document_id=document_id, preprocessing_run_id=preprocessing_run_id, analysis_run_id=analysis_run_id,
        candidate_id=candidate_id, catalog_version_id=catalog_version_id, sku_id=sku_id,
    )


def _evidence(preprocessing_run_id: uuid.UUID) -> EvidenceReference:
    return EvidenceReference(
        preprocessing_run_id=preprocessing_run_id, page_number=1, block_id="native:p0001:b000000",
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
        bounding_box=BoundingBox(x0=1, y0=2, x1=30, y1=12),
    )


def _task(organization_id: uuid.UUID, ids: dict) -> SkuMappingTask:
    return SkuMappingTask(
        schema_version=SKU_MAPPING_TASK_SCHEMA_VERSION, organization_id=organization_id, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        document_analysis_schema_version="analysis-v1", document_analysis_agent_version="a1",
        candidate_id=CANDIDATE_EXTERNAL_ID, raw_name="Premium Support",
        commercial_status=CommercialStatus.PURCHASED, candidate_evidence=(_evidence(ids["preprocessing_run_id"]),),
    )


def _sku_record(organization_id: uuid.UUID, ids: dict) -> SkuRecord:
    return SkuRecord(
        id=ids["sku_id"], source_sku_id=uuid.uuid4(), organization_id=organization_id,
        catalog_version_id=ids["catalog_version_id"], sku_code="PREMIUM_SUPPORT", name="Premium Support",
    )


def _retrieval(organization_id: uuid.UUID, ids: dict, sku: SkuRecord) -> SkuRetrievalResult:
    return SkuRetrievalResult(
        schema_version=SKU_RETRIEVAL_SCHEMA_VERSION, retriever_version=HYBRID_SKU_RETRIEVER_VERSION,
        organization_id=organization_id, catalog_version_id=ids["catalog_version_id"],
        catalog_version_identifier="v1", candidate_id=CANDIDATE_EXTERNAL_ID,
        candidates=(RetrievedSku(sku=sku, score=1.0, matched_sources=(SkuMatchSource.EXACT,), matched_text=sku.name),),
    )


def _match_decision(task: SkuMappingTask, sku: SkuRecord, catalog_version_id: uuid.UUID) -> SkuMappingDecision:
    return SkuMappingDecision(
        schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION, organization_id=task.organization_id,
        document_id=task.document_id, preprocessing_run_id=task.preprocessing_run_id,
        analysis_run_id=task.analysis_run_id, candidate_id=task.candidate_id, catalog_version_id=catalog_version_id,
        outcome=SkuMappingOutcome.MATCH, sku_id=sku.id, sku_code=sku.sku_code, sku_name=sku.name,
        evidence=task.candidate_evidence,
    )


def _runtime(sku: SkuRecord) -> SkuMappingRuntimeSummary:
    return SkuMappingRuntimeSummary(
        request_id=uuid.uuid4(), started_at=datetime.now(UTC), tool_call_count=2,
        tool_calls_by_name={"retrieve_skus": 1, "get_authoritative_sku": 1},
        authoritative_skus_by_id={sku.id: sku}, catalog_version_id=sku.catalog_version_id,
    )


async def _create_run(service: SkuMappingPersistenceService, organization_id: uuid.UUID, ids: dict, *, attempt: int = 1, supersedes: uuid.UUID | None = None) -> uuid.UUID:
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization_id, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], candidate_id=CANDIDATE_EXTERNAL_ID, attempt_number=attempt,
        schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION, document_analysis_schema_version="analysis-v1",
        prompt_version=SKU_MAPPING_PROMPT_VERSION, skill_version=SKU_MAPPING_SKILL_VERSION,
        agent_version=SKU_MAPPING_AGENT_VERSION, retriever_version=HYBRID_SKU_RETRIEVER_VERSION,
        model="test-model", started_at=datetime.now(UTC), supersedes_mapping_run_id=supersedes,
    )
    return run_id


@pytest.mark.asyncio
async def test_completed_match_round_trips_with_decision_and_evidence_projections(tmp_path, organization):
    """Persist gzip canonical JSON and both projections, then reload an identical artifact."""

    ids = await _source(organization)
    storage = LocalObjectStorage(tmp_path)
    service = SkuMappingPersistenceService(get_session_factory(), storage)
    task = _task(organization, ids)
    sku = _sku_record(organization, ids)
    retrieval = _retrieval(organization, ids, sku)
    decision = _match_decision(task, sku, ids["catalog_version_id"])
    artifact = SkuMappingRunArtifact(schema_version=SKU_MAPPING_ARTIFACT_SCHEMA_VERSION, task=task, retrieval=retrieval, decision=decision)
    run_id = await _create_run(service, organization, ids)

    await service.save_completed_result(run_id=run_id, artifact=artifact, runtime=_runtime(sku))
    loaded = await service.load_completed_result(organization_id=organization, run_id=run_id)

    assert loaded == artifact
    async with get_session_factory()() as session:
        run = await session.get(SkuMappingRun, run_id)
        assert run.status == "completed" and run.catalog_version_id == ids["catalog_version_id"]
        projection = await session.scalar(
            select(SkuMappingDecisionProjection).where(SkuMappingDecisionProjection.sku_mapping_run_id == run_id)
        )
        assert projection.outcome == "match" and projection.sku_code == "PREMIUM_SUPPORT"
        evidence_rows = (await session.scalars(
            select(SkuMappingEvidenceReference).where(SkuMappingEvidenceReference.sku_mapping_run_id == run_id)
        )).all()
        assert len(evidence_rows) == 1 and evidence_rows[0].external_block_id == "native:p0001:b000000"


@pytest.mark.asyncio
async def test_mark_run_failed_records_safe_diagnostics_and_clears_artifact_fields(tmp_path, organization):
    """Record a bounded failure without any canonical artifact ever being written."""

    ids = await _source(organization)
    service = SkuMappingPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    run_id = await _create_run(service, organization, ids)

    await service.mark_run_failed(run_id, error_code="sku_mapping_timeout", error_message="took too long")

    async with get_session_factory()() as session:
        run = await session.get(SkuMappingRun, run_id)
        assert run.status == "failed"
        assert run.error_code == "sku_mapping_timeout"
        assert run.canonical_result_storage_key is None


@pytest.mark.asyncio
async def test_save_completed_result_rejects_when_completion_gate_fails(tmp_path, organization):
    """Refuse to persist a decision whose own retrieval never actually ran."""

    ids = await _source(organization)
    service = SkuMappingPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    task = _task(organization, ids)
    sku = _sku_record(organization, ids)
    retrieval = _retrieval(organization, ids, sku)
    decision = _match_decision(task, sku, ids["catalog_version_id"])
    artifact = SkuMappingRunArtifact(schema_version=SKU_MAPPING_ARTIFACT_SCHEMA_VERSION, task=task, retrieval=retrieval, decision=decision)
    run_id = await _create_run(service, organization, ids)
    empty_runtime = SkuMappingRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC))

    with pytest.raises(SkuMappingPersistenceError):
        await service.save_completed_result(run_id=run_id, artifact=artifact, runtime=empty_runtime)


@pytest.mark.asyncio
async def test_save_completed_result_rejects_a_sku_deactivated_since_confirmation(tmp_path, organization):
    """Refuse a MATCH whose SKU went inactive in the live catalog after the agent confirmed it.

    The decision and runtime are internally consistent with each other (so
    the pure `validate_sku_mapping_decision` gate passes) — this specifically
    exercises persistence's own independent re-check against the live
    database, catching staleness between confirmation time and save time
    that the pure, DB-free validator cannot see.
    """

    ids = await _source(organization)
    service = SkuMappingPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    task = _task(organization, ids)
    sku = _sku_record(organization, ids)
    retrieval = _retrieval(organization, ids, sku)
    decision = _match_decision(task, sku, ids["catalog_version_id"])
    artifact = SkuMappingRunArtifact(schema_version=SKU_MAPPING_ARTIFACT_SCHEMA_VERSION, task=task, retrieval=retrieval, decision=decision)
    run_id = await _create_run(service, organization, ids)

    async with get_session_factory()() as session:
        async with session.begin():
            live_sku = await session.get(Sku, ids["sku_id"])
            live_sku.is_active = False

    with pytest.raises(SkuMappingPersistenceError) as excinfo:
        await service.save_completed_result(run_id=run_id, artifact=artifact, runtime=_runtime(sku))
    assert excinfo.value.code == "mapping_sku_not_confirmed"


@pytest.mark.asyncio
async def test_duplicate_attempt_number_is_rejected(tmp_path, organization):
    """Reject a second run for the same candidate reusing an already-used attempt number."""

    ids = await _source(organization)
    service = SkuMappingPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    await _create_run(service, organization, ids, attempt=1)

    with pytest.raises(SkuMappingPersistenceError) as excinfo:
        await _create_run(service, organization, ids, attempt=1)
    assert excinfo.value.code == "mapping_attempt_exists"


@pytest.mark.asyncio
async def test_create_run_rejects_unknown_candidate(tmp_path, organization):
    """Refuse a run naming a candidate that does not exist in the given analysis run."""

    ids = await _source(organization)
    service = SkuMappingPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))

    with pytest.raises(SkuMappingPersistenceError) as excinfo:
        await service.create_run(
            run_id=uuid.uuid4(), organization_id=organization, document_id=ids["document_id"],
            analysis_run_id=ids["analysis_run_id"], candidate_id="candidate-unknown", attempt_number=1,
            schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION, document_analysis_schema_version="analysis-v1",
            prompt_version=SKU_MAPPING_PROMPT_VERSION, skill_version=SKU_MAPPING_SKILL_VERSION,
            agent_version=SKU_MAPPING_AGENT_VERSION, retriever_version=HYBRID_SKU_RETRIEVER_VERSION,
            model="test-model", started_at=datetime.now(UTC),
        )
    assert excinfo.value.code == "mapping_candidate_not_found"


@pytest.mark.asyncio
async def test_supersedes_requires_matching_lineage(tmp_path, organization):
    """Accept a same-lineage supersession and reject one naming an unrelated run."""

    ids = await _source(organization)
    other_ids = await _source(organization, with_catalog=False)
    service = SkuMappingPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    original_run_id = await _create_run(service, organization, ids, attempt=1)

    # A second attempt for the SAME candidate, superseding the first: allowed.
    superseding_run_id = await _create_run(service, organization, ids, attempt=2, supersedes=original_run_id)
    async with get_session_factory()() as session:
        run = await session.get(SkuMappingRun, superseding_run_id)
        assert run.supersedes_mapping_run_id == original_run_id

    # A run for a DIFFERENT candidate claiming to supersede the first: rejected.
    with pytest.raises(SkuMappingPersistenceError) as excinfo:
        await service.create_run(
            run_id=uuid.uuid4(), organization_id=organization, document_id=other_ids["document_id"],
            analysis_run_id=other_ids["analysis_run_id"], candidate_id=CANDIDATE_EXTERNAL_ID, attempt_number=1,
            schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION, document_analysis_schema_version="analysis-v1",
            prompt_version=SKU_MAPPING_PROMPT_VERSION, skill_version=SKU_MAPPING_SKILL_VERSION,
            agent_version=SKU_MAPPING_AGENT_VERSION, retriever_version=HYBRID_SKU_RETRIEVER_VERSION,
            model="test-model", started_at=datetime.now(UTC), supersedes_mapping_run_id=original_run_id,
        )
    assert excinfo.value.code == "mapping_supersedes_lineage_mismatch"
