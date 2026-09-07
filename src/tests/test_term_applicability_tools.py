"""Test PersistedTermApplicabilityTools' task-bound authorization, with a fake resolver.

No database, no Claude call.
"""

import uuid

import pytest

from maximor.document_analysis.schemas import EvidenceReference, EvidenceRepresentation
from maximor.document_analysis.tool_schemas import BlockResult
from maximor.preprocessing.schemas import BoundingBox, ExtractionSource
from maximor.term_applicability.contracts import CandidateContext, TermApplicabilityTask, TermContext
from maximor.term_applicability.errors import CandidateNotFoundError, EvidenceIdNotFoundError, TaskScopeMismatchError, TermNotFoundError
from maximor.term_applicability.tool_schemas import CandidateEvidenceRegionInput, TermEvidenceRegionInput
from maximor.term_applicability.tools import PersistedTermApplicabilityTools


def _evidence(preprocessing_run_id: uuid.UUID, block_id: str) -> EvidenceReference:
    return EvidenceReference(
        preprocessing_run_id=preprocessing_run_id, page_number=1, block_id=block_id,
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
        bounding_box=BoundingBox(x0=1, y0=2, x1=30, y1=12),
    )


def _task() -> TermApplicabilityTask:
    preprocessing_run_id = uuid.uuid4()
    term_evidence = _evidence(preprocessing_run_id, "native:p0001:b000000")
    candidate_evidence = _evidence(preprocessing_run_id, "native:p0001:b000001")
    from maximor.document_analysis.schemas import CommercialStatus
    return TermApplicabilityTask(
        schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=preprocessing_run_id, analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="a", document_analysis_agent_version="a",
        candidates=(CandidateContext(candidate_id="candidate-0001", raw_name="X", commercial_status=CommercialStatus.PURCHASED, evidence_ids=("native:p0001:b000001",)),),
        terms=(TermContext(term_id="term-0001", raw_name="Y", evidence_ids=("native:p0001:b000000",)),),
        evidence_by_id={"native:p0001:b000000": term_evidence, "native:p0001:b000001": candidate_evidence},
    )


class FakeResolver:
    """Return a fixed BlockResult and record the exact request it received."""

    def __init__(self) -> None:
        self.calls = []

    async def get_evidence_region(self, request):
        self.calls.append(request)
        return BlockResult(
            block_id=request.evidence.block_id, page_number=1, representation=EvidenceRepresentation.NATIVE_TEXT,
            extraction_source=ExtractionSource.NATIVE, reading_order=0, text="ok",
            bounding_box=request.evidence.bounding_box,
        )


@pytest.mark.asyncio
async def test_get_term_evidence_region_resolves_the_terms_own_evidence():
    task = _task()
    resolver = FakeResolver()
    tools = PersistedTermApplicabilityTools(resolver, task)
    result = await tools.get_term_evidence_region(TermEvidenceRegionInput(
        organization_id=task.organization_id, analysis_run_id=task.analysis_run_id,
        term_id="term-0001", evidence_id="native:p0001:b000000",
    ))
    assert result.block_id == "native:p0001:b000000"
    assert resolver.calls[0].evidence.preprocessing_run_id == task.preprocessing_run_id


@pytest.mark.asyncio
async def test_get_candidate_evidence_region_resolves_the_candidates_own_evidence():
    task = _task()
    resolver = FakeResolver()
    tools = PersistedTermApplicabilityTools(resolver, task)
    result = await tools.get_candidate_evidence_region(CandidateEvidenceRegionInput(
        organization_id=task.organization_id, analysis_run_id=task.analysis_run_id,
        candidate_id="candidate-0001", evidence_id="native:p0001:b000001",
    ))
    assert result.block_id == "native:p0001:b000001"


@pytest.mark.asyncio
async def test_term_lookup_rejects_a_term_outside_the_task():
    task = _task()
    tools = PersistedTermApplicabilityTools(FakeResolver(), task)
    with pytest.raises(TermNotFoundError):
        await tools.get_term_evidence_region(TermEvidenceRegionInput(
            organization_id=task.organization_id, analysis_run_id=task.analysis_run_id,
            term_id="term-missing", evidence_id="native:p0001:b000000",
        ))


@pytest.mark.asyncio
async def test_candidate_lookup_rejects_a_candidate_outside_the_task():
    task = _task()
    tools = PersistedTermApplicabilityTools(FakeResolver(), task)
    with pytest.raises(CandidateNotFoundError):
        await tools.get_candidate_evidence_region(CandidateEvidenceRegionInput(
            organization_id=task.organization_id, analysis_run_id=task.analysis_run_id,
            candidate_id="candidate-missing", evidence_id="native:p0001:b000001",
        ))


@pytest.mark.asyncio
async def test_evidence_id_not_belonging_to_the_term_is_rejected():
    """An evidence ID that exists in the task but belongs to a different term/candidate is rejected."""

    task = _task()
    tools = PersistedTermApplicabilityTools(FakeResolver(), task)
    with pytest.raises(EvidenceIdNotFoundError):
        await tools.get_term_evidence_region(TermEvidenceRegionInput(
            organization_id=task.organization_id, analysis_run_id=task.analysis_run_id,
            term_id="term-0001", evidence_id="native:p0001:b000001",  # belongs to the candidate, not this term
        ))


@pytest.mark.asyncio
async def test_arbitrary_evidence_id_is_rejected():
    """An evidence ID never seen by this task at all is rejected, not silently resolved."""

    task = _task()
    tools = PersistedTermApplicabilityTools(FakeResolver(), task)
    with pytest.raises(EvidenceIdNotFoundError):
        await tools.get_term_evidence_region(TermEvidenceRegionInput(
            organization_id=task.organization_id, analysis_run_id=task.analysis_run_id,
            term_id="term-0001", evidence_id="native:p9999:b999999",
        ))


@pytest.mark.asyncio
async def test_mismatched_task_scope_is_rejected():
    """A request scoped to a different organization/analysis run than this fixed task is rejected."""

    task = _task()
    tools = PersistedTermApplicabilityTools(FakeResolver(), task)
    with pytest.raises(TaskScopeMismatchError):
        await tools.get_term_evidence_region(TermEvidenceRegionInput(
            organization_id=uuid.uuid4(), analysis_run_id=task.analysis_run_id,
            term_id="term-0001", evidence_id="native:p0001:b000000",
        ))
    with pytest.raises(TaskScopeMismatchError):
        await tools.get_candidate_evidence_region(CandidateEvidenceRegionInput(
            organization_id=task.organization_id, analysis_run_id=uuid.uuid4(),
            candidate_id="candidate-0001", evidence_id="native:p0001:b000001",
        ))
