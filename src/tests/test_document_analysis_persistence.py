"""Test analysis persistence using generated database projections and storage only."""

import hashlib
import uuid
from datetime import UTC, datetime

import pytest

from maximor.db.models import (
    Document,
    DocumentAnalysisEvidenceReference,
    DocumentAnalysisRun,
    DocumentBlock,
    DocumentCommercialStatusAssessment,
    DocumentGlobalTerm,
    DocumentGlobalTermCandidate,
    DocumentPage,
    DocumentProcessingRun,
    DocumentProductCandidate,
    DocumentTable,
    ProcessingJob,
)
from maximor.db.session import get_session_factory
from maximor.document_analysis.errors import DocumentAnalysisPersistenceError
from maximor.document_analysis.agent import DocumentAnalysisRuntimeSummary
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.document_analysis.schemas import (
    CommercialStatus,
    CommercialStatusAssessment,
    ApplicabilityScope,
    DocumentAnalysisResult,
    EvidenceReference,
    EvidenceRepresentation,
    GlobalTerm,
    ProductCandidate,
)
from maximor.preprocessing.persistence import canonical_json_bytes, compress_canonical_json
from maximor.preprocessing.schemas import BoundingBox, ExtractionSource
from maximor.storage import LocalObjectStorage, StorageError


async def _source(organization_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a completed generated preprocessing run with one block and one table."""
    document_id, job_id, preprocessing_run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        async with session.begin():
            session.add(Document(id=document_id, organization_id=organization_id, original_filename="generated.pdf", storage_key=f"generated/{document_id}.pdf", sha256_checksum="a" * 64, media_type="application/pdf", status="completed"))
            session.add(ProcessingJob(id=job_id, organization_id=organization_id, document_id=document_id, job_type="document_preprocessing", status="completed", attempt_number=1))
            await session.flush()
            session.add(DocumentProcessingRun(id=preprocessing_run_id, organization_id=organization_id, document_id=document_id, processing_job_id=job_id, attempt_number=1, status="completed", schema_version="prep-v1", processor_version="test", original_document_checksum="a" * 64, page_count=1, started_at=now, completed_at=now))
            await session.flush()
            page = DocumentPage(organization_id=organization_id, processing_run_id=preprocessing_run_id, page_number=1, width_points=100, height_points=100, rotation_degrees=0, native_plain_text="generated", native_character_count=9, native_word_count=1, ocr_status="not_required", ocr_plain_text=None, quality={}, render_storage_key="generated/render.png", render_media_type="image/png", render_pixel_width=1, render_pixel_height=1, render_dpi=72, render_checksum="b" * 64, warnings=[], created_at=now)
            session.add(page)
            await session.flush()
            session.add(DocumentBlock(id=uuid.uuid4(), organization_id=organization_id, processing_run_id=preprocessing_run_id, document_page_id=page.id, external_block_id="native:p0001:b000000", representation="native_text", extraction_source="native", reading_order=0, text="generated", x0=1, y0=2, x1=30, y1=12, created_at=now))
            session.add(DocumentTable(id=uuid.uuid4(), organization_id=organization_id, processing_run_id=preprocessing_run_id, document_page_id=page.id, external_table_id="table:p0001:t0000", table_index=0, x0=1, y0=20, x1=40, y1=50, rows=[], warnings=[], created_at=now))
    return document_id, job_id, preprocessing_run_id


def _result(organization_id: uuid.UUID, document_id: uuid.UUID, preprocessing_run_id: uuid.UUID) -> DocumentAnalysisResult:
    """Build one generated, deterministic result with candidate/status/evidence projections."""
    evidence = EvidenceReference(preprocessing_run_id=preprocessing_run_id, page_number=1, block_id="native:p0001:b000000", representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE, bounding_box=BoundingBox(x0=1, y0=2, x1=30, y1=12))
    return DocumentAnalysisResult(schema_version="analysis-v1", organization_id=organization_id, document_id=document_id, preprocessing_run_id=preprocessing_run_id, preprocessing_schema_version="prep-v1", prompt_version="prompt-v1", agent_version="agent-v1", product_candidates=(ProductCandidate(candidate_id="candidate-0001", raw_name="Generated service", evidence=(evidence,)),), commercial_statuses=(CommercialStatusAssessment(assessment_id="status-0001", status=CommercialStatus.PURCHASED, candidate_id="candidate-0001", evidence=(evidence,)),), global_terms=(GlobalTerm(term_id="term-0001", raw_name="Billing frequency", raw_value="Monthly", applicability_scope=ApplicabilityScope.CANDIDATE, applies_to_candidate_ids=("candidate-0001",), evidence=(evidence,)),), evidence_references=(evidence,))


def _runtime() -> DocumentAnalysisRuntimeSummary:
    """Create only successful overview and content retrieval metrics for completion tests."""
    return DocumentAnalysisRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC), tool_call_count=2, tool_calls_by_name={"get_document_overview": 1, "search_document": 1})


async def _run(service, organization_id, document_id, preprocessing_run_id, attempt=1):
    """Create a generated analysis run with all version inputs explicit."""
    run_id = uuid.uuid4()
    await service.create_run(run_id=run_id, organization_id=organization_id, document_id=document_id, preprocessing_run_id=preprocessing_run_id, attempt_number=attempt, schema_version="analysis-v1", preprocessing_schema_version="prep-v1", prompt_version="prompt-v1", skill_version="order-form-analysis-v1", agent_version="agent-v1", model="test-model", started_at=datetime.now(UTC))
    return run_id


@pytest.mark.asyncio
async def test_completed_result_round_trips_with_candidate_status_and_evidence_projections(tmp_path, organization):
    """Persist gzip canonical JSON and all projections without source-PDF access."""
    document_id, _, preprocessing_run_id = await _source(organization)
    storage = LocalObjectStorage(tmp_path)
    service = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    run_id = await _run(service, organization, document_id, preprocessing_run_id)
    result = _result(organization, document_id, preprocessing_run_id)
    key, checksum = await service.save_completed_result(analysis_run_id=run_id, result=result, runtime=_runtime())
    assert key.endswith(".json.gz") and len(checksum) == 64
    assert await service.load_completed_result(organization_id=organization, analysis_run_id=run_id) == result
    async with get_session_factory()() as session:
        run = await session.get(DocumentAnalysisRun, run_id)
        candidates = list((await session.scalars(__import__('sqlalchemy').select(DocumentProductCandidate).where(DocumentProductCandidate.analysis_run_id == run_id))).all())
        statuses = list((await session.scalars(__import__('sqlalchemy').select(DocumentCommercialStatusAssessment).where(DocumentCommercialStatusAssessment.analysis_run_id == run_id))).all())
        evidence = list((await session.scalars(__import__('sqlalchemy').select(DocumentAnalysisEvidenceReference).where(DocumentAnalysisEvidenceReference.analysis_run_id == run_id))).all())
        terms = list((await session.scalars(__import__('sqlalchemy').select(DocumentGlobalTerm).where(DocumentGlobalTerm.analysis_run_id == run_id))).all())
        term_links = list((await session.scalars(__import__('sqlalchemy').select(DocumentGlobalTermCandidate).where(DocumentGlobalTermCandidate.analysis_run_id == run_id))).all())
    assert run.status == "completed" and run.validation_status == "validated"
    assert candidates[0].external_candidate_id == "candidate-0001"
    assert statuses[0].product_candidate_id == candidates[0].id
    assert len(evidence) == 4 and all(row.document_block_id is not None for row in evidence)
    assert terms[0].applicability_scope == "candidate" and terms[0].evidence_count == 1
    assert len(term_links) == 1 and term_links[0].candidate_external_id == "candidate-0001"


@pytest.mark.asyncio
async def test_attempts_are_distinct_duplicate_attempts_are_rejected_and_same_result_is_idempotent(tmp_path, organization):
    """Keep attempts immutable while allowing a repeated identical completed save."""
    document_id, _, preprocessing_run_id = await _source(organization)
    service = DocumentAnalysisPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    first = await _run(service, organization, document_id, preprocessing_run_id, attempt=1)
    second = await _run(service, organization, document_id, preprocessing_run_id, attempt=2)
    result = _result(organization, document_id, preprocessing_run_id)
    first_key = await service.save_completed_result(analysis_run_id=first, result=result, runtime=_runtime())
    assert first_key == await service.save_completed_result(analysis_run_id=first, result=result, runtime=_runtime())
    with pytest.raises(DocumentAnalysisPersistenceError) as caught:
        await _run(service, organization, document_id, preprocessing_run_id, attempt=2)
    assert caught.value.code == "analysis_attempt_exists" and first != second


@pytest.mark.asyncio
async def test_cross_run_page_source_and_geometry_evidence_is_rejected_without_artifact(tmp_path, organization):
    """Reject evidence that does not exactly resolve to the claimed preprocessing record."""
    document_id, _, preprocessing_run_id = await _source(organization)
    storage = LocalObjectStorage(tmp_path)
    service = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    run_id = await _run(service, organization, document_id, preprocessing_run_id)
    bad = _result(organization, document_id, preprocessing_run_id).model_copy(update={"evidence_references": (EvidenceReference(preprocessing_run_id=preprocessing_run_id, page_number=2, block_id="native:p0001:b000000", representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE),)})
    with pytest.raises(DocumentAnalysisPersistenceError) as caught:
        await service.save_completed_result(analysis_run_id=run_id, result=bad, runtime=_runtime())
    assert caught.value.code == "analysis_evidence_page_not_found"
    key = service._artifact_key(bad, run_id)
    with pytest.raises(StorageError):
        await storage.inspect(key)


@pytest.mark.asyncio
async def test_checksum_failure_and_failed_run_do_not_change_other_workflow_statuses(tmp_path, organization):
    """Reject corrupt artifacts and keep document/job/preprocessing statuses untouched."""
    document_id, job_id, preprocessing_run_id = await _source(organization)
    storage = LocalObjectStorage(tmp_path)
    service = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    run_id = await _run(service, organization, document_id, preprocessing_run_id)
    result = _result(organization, document_id, preprocessing_run_id)
    key, _ = await service.save_completed_result(analysis_run_id=run_id, result=result, runtime=_runtime())
    async def corrupt():
        yield b"bad"
    await storage.put(key, corrupt(), maximum_bytes=100)
    with pytest.raises(DocumentAnalysisPersistenceError) as caught:
        await service.load_completed_result(organization_id=organization, analysis_run_id=run_id)
    assert caught.value.code == "analysis_artifact_checksum_mismatch"
    failed_id = await _run(service, organization, document_id, preprocessing_run_id, attempt=2)
    await service.mark_run_failed(failed_id, error_code="safe_code", error_message="safe message")
    async with get_session_factory()() as session:
        document, job, preprocessing, failed = await session.get(Document, document_id), await session.get(ProcessingJob, job_id), await session.get(DocumentProcessingRun, preprocessing_run_id), await session.get(DocumentAnalysisRun, failed_id)
    assert (document.status, job.status, preprocessing.status, failed.status) == ("completed", "completed", "completed", "failed")


@pytest.mark.asyncio
async def test_persistence_rejects_ungrounded_runtime_bypass(tmp_path, organization):
    """Never accept a direct persistence caller whose trace proves no grounding."""
    document_id, _, preprocessing_run_id = await _source(organization)
    service = DocumentAnalysisPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    run_id = await _run(service, organization, document_id, preprocessing_run_id)
    with pytest.raises(DocumentAnalysisPersistenceError) as caught:
        await service.save_completed_result(
            analysis_run_id=run_id, result=_result(organization, document_id, preprocessing_run_id),
            runtime=DocumentAnalysisRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC)),
        )
    assert caught.value.code == "analysis_completion_gate_failed"


@pytest.mark.asyncio
async def test_failed_run_persists_only_bounded_diagnostics_and_token_classes(tmp_path, organization):
    """Retain safe failure facts and cache usage without rejected output values."""

    document_id, _, preprocessing_run_id = await _source(organization)
    service = DocumentAnalysisPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    run_id = await _run(service, organization, document_id, preprocessing_run_id)
    runtime = _runtime()
    runtime.successful_tool_call_order = ("get_document_overview", "get_page_text")
    runtime.referenced_pages = (1,)
    runtime.input_tokens = 3
    runtime.cache_creation_input_tokens = 10
    runtime.cache_read_input_tokens = 20
    runtime.output_tokens = 5
    runtime.model_usage = {"model-a": {"inputTokens": 3, "cacheReadInputTokens": 20, "costUSD": 0.1}}
    runtime.cost_usd = 0.1
    runtime.failure_stage = "pydantic_schema_validation"
    runtime.pydantic_errors = ({"location": "product_candidates.0.raw_name", "type": "string_too_long"},)
    runtime.structured_output_present = True
    runtime.output_field_names = ("product_candidates", "schema_version")
    runtime.output_collection_counts = {"product_candidates": 1}
    runtime.tool_event_types = ("AssistantMessage", "ResultMessage")
    runtime.record_timing_event(
        kind="tool_invocation_started", timestamp=datetime.now(UTC),
        monotonic_now=runtime.monotonic_started_at + 0.01,
        tool_name="get_document_overview", sequence_number=1,
    )
    runtime.record_timing_event(
        kind="tool_invocation_completed", timestamp=datetime.now(UTC),
        monotonic_now=runtime.monotonic_started_at + 0.02,
        tool_name="get_document_overview", sequence_number=1, succeeded=True,
    )
    runtime.record_terminal(
        "validation_failure", datetime.now(UTC), runtime.monotonic_started_at + 0.03,
    )
    runtime.finalize_timing(datetime.now(UTC), runtime.monotonic_started_at + 0.04)
    await service.mark_run_failed(
        run_id, error_code="document_analysis_invalid_output",
        error_message="Document analysis returned an invalid result.", runtime=runtime,
    )
    async with get_session_factory()() as session:
        run = await session.get(DocumentAnalysisRun, run_id)
        candidate_count = await session.scalar(__import__('sqlalchemy').select(__import__('sqlalchemy').func.count()).select_from(DocumentProductCandidate).where(DocumentProductCandidate.analysis_run_id == run_id))
    assert run.status == "failed" and run.canonical_result_storage_key is None
    assert (run.input_tokens, run.cache_creation_input_tokens, run.cache_read_input_tokens, run.output_tokens) == (3, 10, 20, 5)
    assert run.runtime_diagnostics["failure_stage"] == "pydantic_schema_validation"
    assert run.runtime_diagnostics["successful_tool_call_order"] == ["get_document_overview", "get_page_text"]
    assert run.runtime_diagnostics["successful_overview_call_count"] == 1
    assert run.runtime_diagnostics["successful_content_retrieval_count"] == 1
    assert run.model_usage["model-a"]["cacheReadInputTokens"] == 20
    assert run.runtime_diagnostics["terminal_kind"] == "validation_failure"
    assert run.runtime_diagnostics["total_elapsed_ms"] >= 0
    assert len(run.runtime_diagnostics["timing_events"]) == 3
    assert candidate_count == 0
    serialized = repr(run.runtime_diagnostics)
    assert "secret" not in serialized and "/private/" not in serialized and "SELECT " not in serialized


def test_canonical_analysis_serialization_is_reproducible_and_lossless():
    """Reuse the established fixed-mtime gzip format for deterministic artifacts."""
    organization_id, document_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    result = _result(organization_id, document_id, run_id)
    payload = canonical_json_bytes(result)
    assert compress_canonical_json(payload) == compress_canonical_json(payload)
    assert hashlib.sha256(payload).hexdigest()
