"""Regression tests for the PersistedTermApplicabilityTools evidence-resolver wiring defect.

Root cause (confirmed by a read-only local reproduction against a real
persisted failed run, never repeated here): `TermApplicabilityHandler`
constructed `PersistedTermApplicabilityTools` with a bare
`DocumentAnalysisRepository` -- which has no `get_evidence_region` method --
instead of first wrapping it in `PersistedDocumentTools`, exactly the
pattern `SkuMappingHandler` already uses correctly
(`PersistedDocumentTools(DocumentAnalysisRepository(sessions), storage)`).
Every real evidence-tool call therefore failed with a plain `AttributeError`
inside the tool implementation itself (schema validation of the *inputs*
always succeeded first), which the agent's adapter wrapper categorizes as a
generic `adapter_error` -- with zero evidence ever marked retrieved. Fixed
by wrapping the repository in `PersistedDocumentTools` before handing it to
`PersistedTermApplicabilityTools`, mirroring `SkuMappingHandler` exactly.

`PersistedTermApplicabilityTools`'s own authorization logic (task-scope,
term/candidate/evidence-id ownership) was never the problem and is already
covered by `test_term_applicability_tools.py`'s fake-resolver tests; this
file instead exercises the *real* `EvidenceResolver` composition
(`PersistedDocumentTools` + `DocumentAnalysisRepository` + real persisted
`document_blocks`/`document_tables` rows) end to end, including through the
real `TermApplicabilityHandler` construction path and the MCP-adapter path
in `ClaudeTermApplicabilityAgent._build_adapters`. Generated fixtures only;
no supplied documents, no Claude call.
"""
import uuid
from datetime import UTC, datetime

import pytest

from maximor.config import get_database_settings
from maximor.db.models import Document, DocumentBlock, DocumentPage, DocumentProcessingRun, DocumentTable, ProcessingJob
from maximor.db.session import get_session_factory
from maximor.document_analysis.agent import DocumentAnalysisRuntimeSummary
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.document_analysis.repository import DocumentAnalysisRepository
from maximor.document_analysis.schemas import (
    CommercialStatus,
    CommercialStatusAssessment,
    DocumentAnalysisResult,
    EvidenceReference,
    EvidenceRepresentation,
    GlobalTerm,
    ProductCandidate,
)
from maximor.document_analysis.tool_schemas import BlockResult, TableResult
from maximor.document_analysis.tools import PersistedDocumentTools
from maximor.jobs.service import schedule_term_applicability_job
from maximor.jobs.types import JobContext, JobType
from maximor.preprocessing.schemas import ExtractionSource
from maximor.storage import LocalObjectStorage
from maximor.term_applicability.agent import ClaudeTermApplicabilityAgent, TermApplicabilityExecution, TermApplicabilityRuntimeSummary
from maximor.term_applicability.contracts import build_term_applicability_task
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
from maximor.term_applicability.schemas import TermApplicabilityDecision, TermApplicabilityResult, TermDisposition
from maximor.term_applicability.tool_schemas import CandidateEvidenceRegionInput, TermEvidenceRegionInput
from maximor.term_applicability.tools import PersistedTermApplicabilityTools
from maximor.term_applicability.versions import TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, TERM_APPLICABILITY_TASK_SCHEMA_VERSION
from maximor.term_triage.agent import TermTriageExecution, TermTriageRuntimeSummary
from maximor.term_triage.schemas import TermTriageDecision, TermTriageDisposition, TermTriageResult
from maximor.term_triage.versions import TERM_TRIAGE_RESULT_SCHEMA_VERSION
from maximor.worker.handlers.term_applicability import TermApplicabilityHandler


async def _seed_with_term_and_table(organization_id: uuid.UUID, storage: LocalObjectStorage) -> dict:
    """Create a completed generated analysis with one term (block) and one candidate (block + table).

    Mirrors `test_document_analysis_persistence._source`'s block+table
    bootstrap and `test_sku_mapping_worker_integration._seed`'s
    candidate/status pattern, but also seeds one `GlobalTerm` -- the sibling
    `_seed` helper used elsewhere has zero global terms, which cannot
    exercise `get_term_evidence_region` at all.
    """

    document_id, job_id, preprocessing_run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    analysis_run_id = uuid.uuid4()
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
            session.add(DocumentBlock(id=uuid.uuid4(), organization_id=organization_id, processing_run_id=preprocessing_run_id, document_page_id=page.id, external_block_id="native:p0001:b000000", representation="native_text", extraction_source="native", reading_order=0, text="generated term text", x0=1, y0=2, x1=30, y1=12, created_at=now))
            session.add(DocumentBlock(id=uuid.uuid4(), organization_id=organization_id, processing_run_id=preprocessing_run_id, document_page_id=page.id, external_block_id="native:p0001:b000001", representation="native_text", extraction_source="native", reading_order=1, text="generated candidate text", x0=1, y0=15, x1=30, y1=19, created_at=now))
            session.add(DocumentTable(id=uuid.uuid4(), organization_id=organization_id, processing_run_id=preprocessing_run_id, document_page_id=page.id, external_table_id="table:p0001:t0000", table_index=0, x0=1, y0=20, x1=40, y1=50, rows=[], warnings=[], created_at=now))

    def _block_evidence(block_id: str) -> EvidenceReference:
        return EvidenceReference(
            preprocessing_run_id=preprocessing_run_id, page_number=1, block_id=block_id,
            representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
        )

    def _table_evidence(table_id: str) -> EvidenceReference:
        return EvidenceReference(
            preprocessing_run_id=preprocessing_run_id, page_number=1, table_id=table_id,
            representation=EvidenceRepresentation.TABLE,
        )

    term = GlobalTerm(term_id="term-0001", raw_name="Payment terms", raw_value="Net 45", evidence=(_block_evidence("native:p0001:b000000"),))
    candidate = ProductCandidate(candidate_id="candidate-0001", raw_name="Premium Support", evidence=(_block_evidence("native:p0001:b000001"), _table_evidence("table:p0001:t0000")))
    status = CommercialStatusAssessment(assessment_id="status-0001", status=CommercialStatus.PURCHASED, candidate_id="candidate-0001", evidence=(_block_evidence("native:p0001:b000001"),))
    result = DocumentAnalysisResult(
        schema_version="analysis-v1", organization_id=organization_id, document_id=document_id,
        preprocessing_run_id=preprocessing_run_id, preprocessing_schema_version="prep-v1",
        prompt_version="p1", agent_version="a1", product_candidates=(candidate,),
        commercial_statuses=(status,), global_terms=(term,),
    )
    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    await analysis_results.create_run(
        run_id=analysis_run_id, organization_id=organization_id, document_id=document_id,
        preprocessing_run_id=preprocessing_run_id, attempt_number=1, schema_version="analysis-v1",
        preprocessing_schema_version="prep-v1", prompt_version="p1", skill_version="s1", agent_version="a1",
        model="test-model", started_at=now,
    )
    runtime = DocumentAnalysisRuntimeSummary(
        request_id=uuid.uuid4(), started_at=now, tool_call_count=2,
        tool_calls_by_name={"get_document_overview": 1, "search_document": 1},
    )
    await analysis_results.save_completed_result(analysis_run_id=analysis_run_id, result=result, runtime=runtime)
    return dict(document_id=document_id, preprocessing_run_id=preprocessing_run_id, analysis_run_id=analysis_run_id)


def _real_resolver(storage: LocalObjectStorage) -> PersistedDocumentTools:
    """Build the correctly-wrapped resolver `TermApplicabilityHandler` must use."""

    return PersistedDocumentTools(DocumentAnalysisRepository(get_session_factory()), storage)


# --- real resolver composition: text-block and table evidence -----------------------

@pytest.mark.asyncio
async def test_real_resolver_resolves_term_text_block_evidence(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _seed_with_term_and_table(organization, storage)
    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    analysis = await analysis_results.load_completed_result(organization_id=organization, analysis_run_id=ids["analysis_run_id"])
    task = build_term_applicability_task(result=analysis, analysis_run_id=ids["analysis_run_id"], schema_version=TERM_APPLICABILITY_TASK_SCHEMA_VERSION)
    tools = PersistedTermApplicabilityTools(_real_resolver(storage), task)

    result = await tools.get_term_evidence_region(TermEvidenceRegionInput(
        organization_id=task.organization_id, analysis_run_id=task.analysis_run_id,
        term_id="term-0001", evidence_id="native:p0001:b000000",
    ))
    assert isinstance(result, BlockResult)
    assert result.block_id == "native:p0001:b000000"


@pytest.mark.asyncio
async def test_real_resolver_resolves_candidate_text_block_evidence(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _seed_with_term_and_table(organization, storage)
    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    analysis = await analysis_results.load_completed_result(organization_id=organization, analysis_run_id=ids["analysis_run_id"])
    task = build_term_applicability_task(result=analysis, analysis_run_id=ids["analysis_run_id"], schema_version=TERM_APPLICABILITY_TASK_SCHEMA_VERSION)
    tools = PersistedTermApplicabilityTools(_real_resolver(storage), task)

    result = await tools.get_candidate_evidence_region(CandidateEvidenceRegionInput(
        organization_id=task.organization_id, analysis_run_id=task.analysis_run_id,
        candidate_id="candidate-0001", evidence_id="native:p0001:b000001",
    ))
    assert isinstance(result, BlockResult)
    assert result.block_id == "native:p0001:b000001"


@pytest.mark.asyncio
async def test_real_resolver_resolves_candidate_table_evidence(tmp_path, organization):
    """Table evidence -- never exercised by the failed real run -- also resolves through the real resolver."""

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed_with_term_and_table(organization, storage)
    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    analysis = await analysis_results.load_completed_result(organization_id=organization, analysis_run_id=ids["analysis_run_id"])
    task = build_term_applicability_task(result=analysis, analysis_run_id=ids["analysis_run_id"], schema_version=TERM_APPLICABILITY_TASK_SCHEMA_VERSION)
    tools = PersistedTermApplicabilityTools(_real_resolver(storage), task)

    result = await tools.get_candidate_evidence_region(CandidateEvidenceRegionInput(
        organization_id=task.organization_id, analysis_run_id=task.analysis_run_id,
        candidate_id="candidate-0001", evidence_id="table:p0001:t0000",
    ))
    assert isinstance(result, TableResult)
    assert result.table_id == "table:p0001:t0000"
    assert isinstance(result.rows, tuple)


@pytest.mark.asyncio
async def test_bare_repository_resolver_reproduces_the_original_defect(tmp_path, organization):
    """A resolver that is a bare `DocumentAnalysisRepository` (the original bug) fails with AttributeError.

    Kept as an explicit negative regression: if the correct
    `PersistedDocumentTools` wrapping is ever accidentally removed from
    `TermApplicabilityHandler` again, this documents exactly what breaks and
    why, without asserting anything about the fixed production code path
    itself (that is covered by the tests above and by
    `test_term_applicability_handler_evidence_resolver_wiring_is_correct`
    below).
    """

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed_with_term_and_table(organization, storage)
    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    analysis = await analysis_results.load_completed_result(organization_id=organization, analysis_run_id=ids["analysis_run_id"])
    task = build_term_applicability_task(result=analysis, analysis_run_id=ids["analysis_run_id"], schema_version=TERM_APPLICABILITY_TASK_SCHEMA_VERSION)
    broken_tools = PersistedTermApplicabilityTools(DocumentAnalysisRepository(get_session_factory()), task)

    with pytest.raises(AttributeError):
        await broken_tools.get_term_evidence_region(TermEvidenceRegionInput(
            organization_id=task.organization_id, analysis_run_id=task.analysis_run_id,
            term_id="term-0001", evidence_id="native:p0001:b000000",
        ))


# --- MCP-adapter path: success/failure and retrieval-tracking timing ----------------

@pytest.mark.asyncio
async def test_mcp_adapter_records_retrieval_only_after_a_successful_real_resolver_return(tmp_path, organization):
    """The agent's adapter marks evidence retrieved only once the real resolver actually returns."""

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed_with_term_and_table(organization, storage)
    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    analysis = await analysis_results.load_completed_result(organization_id=organization, analysis_run_id=ids["analysis_run_id"])
    task = build_term_applicability_task(result=analysis, analysis_run_id=ids["analysis_run_id"], schema_version=TERM_APPLICABILITY_TASK_SCHEMA_VERSION)
    tools = PersistedTermApplicabilityTools(_real_resolver(storage), task)

    settings = get_database_settings()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings)._build_adapters(task, tools)
    term_adapter, candidate_adapter = adapters[0], adapters[1]

    assert runtime.retrieved_term_evidence_ids == {}
    response = await term_adapter.handler({"term_id": "term-0001", "evidence_id": "native:p0001:b000000"})
    assert not response.get("is_error")
    assert runtime.retrieved_term_evidence_ids == {"term-0001": frozenset({"native:p0001:b000000"})}

    assert runtime.retrieved_candidate_evidence_ids == {}
    response = await candidate_adapter.handler({"candidate_id": "candidate-0001", "evidence_id": "table:p0001:t0000"})
    assert not response.get("is_error")
    assert runtime.retrieved_candidate_evidence_ids == {"candidate-0001": frozenset({"table:p0001:t0000"})}


class _AlwaysFailingResolver:
    """Simulate a resolver whose underlying call always raises, never a successful return."""

    async def get_evidence_region(self, request):
        raise RuntimeError("simulated resolver failure")


@pytest.mark.asyncio
async def test_mcp_adapter_never_marks_retrieval_when_the_resolver_itself_fails(tmp_path, organization):
    """A resolver failure is a safe adapter error and never marks evidence retrieved."""

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed_with_term_and_table(organization, storage)
    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    analysis = await analysis_results.load_completed_result(organization_id=organization, analysis_run_id=ids["analysis_run_id"])
    task = build_term_applicability_task(result=analysis, analysis_run_id=ids["analysis_run_id"], schema_version=TERM_APPLICABILITY_TASK_SCHEMA_VERSION)
    tools = PersistedTermApplicabilityTools(_AlwaysFailingResolver(), task)

    settings = get_database_settings()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings)._build_adapters(task, tools)
    term_adapter = adapters[0]

    response = await term_adapter.handler({"term_id": "term-0001", "evidence_id": "native:p0001:b000000"})
    assert response.get("is_error")
    assert runtime.retrieved_term_evidence_ids == {}
    assert runtime.failed_tool_call_count == 1


# --- the actual regression: TermApplicabilityHandler's own construction ------------

class _EvidenceCallingApplicabilityAgent:
    """Stand in for ClaudeTermApplicabilityAgent, but actually call the handler-built tools.

    This is the one test that exercises `TermApplicabilityHandler.execute`'s
    own `PersistedTermApplicabilityTools(...)` construction line exactly as
    production code builds it -- the only place the original defect lived.
    """

    async def execute(self, task, tools) -> TermApplicabilityExecution:
        term_result = await tools.get_term_evidence_region(TermEvidenceRegionInput(
            organization_id=task.organization_id, analysis_run_id=task.analysis_run_id,
            term_id="term-0001", evidence_id="native:p0001:b000000",
        ))
        candidate_result = await tools.get_candidate_evidence_region(CandidateEvidenceRegionInput(
            organization_id=task.organization_id, analysis_run_id=task.analysis_run_id,
            candidate_id="candidate-0001", evidence_id="table:p0001:t0000",
        ))
        assert isinstance(term_result, BlockResult)
        assert isinstance(candidate_result, TableResult)
        # Every triage-selected term needs exactly one decision; use
        # document_metadata (no scope/evidence required) since this test
        # exercises evidence *resolution*, not applicability semantics.
        decisions = tuple(
            TermApplicabilityDecision(schema_version="1.0.0", term_id=term_id, disposition=TermDisposition.DOCUMENT_METADATA)
            for term_id in task.effective_selected_term_ids
        )
        result = TermApplicabilityResult(
            schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION,
            organization_id=task.organization_id, document_id=task.document_id,
            preprocessing_run_id=task.preprocessing_run_id, analysis_run_id=task.analysis_run_id,
            decisions=decisions, candidate_commercial_facts=(), candidate_commercial_fact_coverage=(),
        )
        runtime = TermApplicabilityRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC), finalization_accepted=True)
        return TermApplicabilityExecution(result=result, runtime=runtime)


class _TrivialTriageAgent:
    async def execute(self, task) -> TermTriageExecution:
        # `rationale` is nullable in the schema but the `term_triage_decisions`
        # table currently enforces NOT NULL -- an unrelated, pre-existing
        # persistence-schema gap, out of scope for this evidence-resolver fix.
        # Supply one so this test exercises only the resolver path.
        decisions = tuple(
            TermTriageDecision(term_id=term.term_id, disposition=TermTriageDisposition.UNCERTAIN, rationale="generated")
            for term in task.terms
        )
        result = TermTriageResult(
            schema_version=TERM_TRIAGE_RESULT_SCHEMA_VERSION,
            organization_id=task.organization_id, document_id=task.document_id,
            preprocessing_run_id=task.preprocessing_run_id, analysis_run_id=task.analysis_run_id,
            decisions=decisions,
        )
        runtime = TermTriageRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC), finalization_accepted=True)
        return TermTriageExecution(result=result, runtime=runtime)


@pytest.mark.asyncio
async def test_term_applicability_handler_evidence_resolver_wiring_is_correct(tmp_path, organization):
    """The exact bug: TermApplicabilityHandler must hand the applicability agent a resolver that works.

    Before the fix, this test would fail with `AttributeError:
    'DocumentAnalysisRepository' object has no attribute 'get_evidence_region'`
    raised from inside `_EvidenceCallingApplicabilityAgent.execute` --
    exactly reproducing the real end-to-end run's failure.
    """

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed_with_term_and_table(organization, storage)
    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    enrichment_results = TermApplicabilityPersistenceService(get_session_factory(), storage)
    handler = TermApplicabilityHandler(
        get_database_settings(), get_session_factory(), storage, analysis_results, enrichment_results,
        triage_agent=_TrivialTriageAgent(), applicability_agent=_EvidenceCallingApplicabilityAgent(),
    )
    job = await schedule_term_applicability_job(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"], analysis_run_id=ids["analysis_run_id"],
    )
    context = JobContext(organization_id=organization, document_id=ids["document_id"], processing_job_id=job.id, job_type=JobType.TERM_APPLICABILITY.value, attempt_number=1)

    await handler.execute(context)  # raises JobExecutionError (or AttributeError, pre-fix) on failure

    run_id = uuid.uuid5(job.id, "1")
    loaded = await enrichment_results.load_completed_result(organization_id=organization, run_id=run_id)
    assert loaded.applicability.organization_id == organization
