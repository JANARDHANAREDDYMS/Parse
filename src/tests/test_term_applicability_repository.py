"""Test tenant/run-scoped repository loading against generated database fixtures.

No supplied PDFs, previews, manifests, or ground truth are used anywhere in
this file, and no Claude call is made.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from maximor.db.models import (
    Document,
    DocumentBlock,
    DocumentPage,
    DocumentProcessingRun,
    DocumentProductCandidate,
    ProcessingJob,
)
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
    GlobalTerm,
    ProductCandidate,
)
from maximor.storage import LocalObjectStorage
from maximor.term_applicability.errors import (
    TermApplicabilityAnalysisRunNotCompletedError,
    TermApplicabilityAnalysisRunNotFoundError,
    TermApplicabilitySourceMismatchError,
)
from maximor.term_applicability.repository import TermApplicabilityRepository
from maximor.term_applicability.versions import TERM_APPLICABILITY_TASK_SCHEMA_VERSION


async def _seed(organization_id: uuid.UUID, storage: LocalObjectStorage, *, completed: bool = True) -> dict:
    """Create one preprocessing+analysis run with one candidate and one term."""

    document_id, job_id, preprocessing_run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    analysis_run_id = uuid.uuid4()
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

    evidence = EvidenceReference(
        preprocessing_run_id=preprocessing_run_id, page_number=1, block_id="native:p0001:b000000",
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    )
    result = DocumentAnalysisResult(
        schema_version="analysis-v1", organization_id=organization_id, document_id=document_id,
        preprocessing_run_id=preprocessing_run_id, preprocessing_schema_version="prep-v1",
        prompt_version="p1", agent_version="a1",
        product_candidates=(ProductCandidate(candidate_id="candidate-0001", raw_name="Premium Support", evidence=(evidence,)),),
        commercial_statuses=(CommercialStatusAssessment(assessment_id="status-0001", status=CommercialStatus.PURCHASED, candidate_id="candidate-0001", evidence=(evidence,)),),
        global_terms=(GlobalTerm(term_id="term-0001", raw_name="Payment terms", raw_value="Net 45", evidence=(evidence,)),),
    )
    analysis_persistence = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    await analysis_persistence.create_run(
        run_id=analysis_run_id, organization_id=organization_id, document_id=document_id,
        preprocessing_run_id=preprocessing_run_id, attempt_number=1, schema_version="analysis-v1",
        preprocessing_schema_version="prep-v1", prompt_version="p1", skill_version="s1", agent_version="a1",
        model="test-model", started_at=now,
    )
    if completed:
        runtime = DocumentAnalysisRuntimeSummary(
            request_id=uuid.uuid4(), started_at=now, tool_call_count=2,
            tool_calls_by_name={"get_document_overview": 1, "search_document": 1},
        )
        await analysis_persistence.save_completed_result(analysis_run_id=analysis_run_id, result=result, runtime=runtime)

    async with get_session_factory()() as session:
        candidate_row = await session.scalar(
            select(DocumentProductCandidate).where(DocumentProductCandidate.analysis_run_id == analysis_run_id)
        )
    return dict(
        document_id=document_id, preprocessing_run_id=preprocessing_run_id, analysis_run_id=analysis_run_id,
        candidate_id=candidate_row.id if candidate_row is not None else None, analysis_results=analysis_persistence,
    )


@pytest.mark.asyncio
async def test_load_task_succeeds_for_a_completed_tenant_scoped_run(tmp_path, organization):
    """A matching, completed run builds a task with the seeded candidate and term."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    repository = TermApplicabilityRepository(get_session_factory(), ids["analysis_results"])
    task = await repository.load_task(
        organization_id=organization, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        schema_version=TERM_APPLICABILITY_TASK_SCHEMA_VERSION,
    )
    assert task.candidate_ids == ("candidate-0001",)
    assert task.term_ids == ("term-0001",)


@pytest.mark.asyncio
async def test_load_task_rejects_unknown_analysis_run(tmp_path, organization):
    """A random analysis_run_id for a real organization is rejected as not found."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    repository = TermApplicabilityRepository(get_session_factory(), ids["analysis_results"])
    with pytest.raises(TermApplicabilityAnalysisRunNotFoundError):
        await repository.load_task(
            organization_id=organization, document_id=ids["document_id"],
            preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=uuid.uuid4(),
            schema_version="1.0.0",
        )


@pytest.mark.asyncio
async def test_load_task_rejects_cross_tenant_analysis_run(tmp_path, organization):
    """A real analysis_run_id requested under the wrong organization is rejected."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    repository = TermApplicabilityRepository(get_session_factory(), ids["analysis_results"])
    with pytest.raises(TermApplicabilityAnalysisRunNotFoundError):
        await repository.load_task(
            organization_id=uuid.uuid4(), document_id=ids["document_id"],
            preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
            schema_version="1.0.0",
        )


@pytest.mark.asyncio
async def test_load_task_rejects_cross_document_analysis_run(tmp_path, organization):
    """A real analysis_run_id requested under the wrong document is rejected."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    repository = TermApplicabilityRepository(get_session_factory(), ids["analysis_results"])
    with pytest.raises(TermApplicabilityAnalysisRunNotFoundError):
        await repository.load_task(
            organization_id=organization, document_id=uuid.uuid4(),
            preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
            schema_version="1.0.0",
        )


@pytest.mark.asyncio
async def test_load_task_rejects_a_not_yet_completed_run(tmp_path, organization):
    """A `running` (not-yet-completed) analysis run is rejected explicitly."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path), completed=False)
    repository = TermApplicabilityRepository(get_session_factory(), ids["analysis_results"])
    with pytest.raises(TermApplicabilityAnalysisRunNotCompletedError):
        await repository.load_task(
            organization_id=organization, document_id=ids["document_id"],
            preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
            schema_version="1.0.0",
        )


@pytest.mark.asyncio
async def test_load_task_rejects_mismatched_preprocessing_run_id(tmp_path, organization):
    """A completed run requested with the wrong preprocessing_run_id is rejected."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    repository = TermApplicabilityRepository(get_session_factory(), ids["analysis_results"])
    with pytest.raises(TermApplicabilitySourceMismatchError):
        await repository.load_task(
            organization_id=organization, document_id=ids["document_id"],
            preprocessing_run_id=uuid.uuid4(), analysis_run_id=ids["analysis_run_id"],
            schema_version="1.0.0",
        )
