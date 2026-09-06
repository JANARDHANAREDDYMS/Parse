"""Test deterministic analysis completion gating without Claude or document access."""

import uuid
from datetime import UTC, datetime

from maximor.document_analysis.agent import DocumentAnalysisRuntimeSummary
from maximor.document_analysis.contracts import DocumentAnalysisRequest
from maximor.document_analysis.schemas import DocumentAnalysisResult, EvidenceReference, EvidenceRepresentation, ProductCandidate
from maximor.document_analysis.validation import validate_document_analysis_result
from maximor.preprocessing.schemas import ExtractionSource


def _request() -> DocumentAnalysisRequest:
    """Build generated trusted identifiers for gate-only tests."""
    return DocumentAnalysisRequest(organization_id=uuid.uuid4(), document_id=uuid.uuid4(), preprocessing_run_id=uuid.uuid4(), preprocessing_schema_version="prep", document_analysis_schema_version="analysis", prompt_version="prompt", skill_version="skill", agent_version="agent")


def _runtime(**calls: int) -> DocumentAnalysisRuntimeSummary:
    """Create a trace containing successful calls only."""
    return DocumentAnalysisRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC), tool_call_count=sum(calls.values()), tool_calls_by_name=calls)


def _result(request: DocumentAnalysisRequest, *, grounded: bool = True) -> DocumentAnalysisResult:
    """Build a generated evidence-backed semantic result or an explicit empty result."""
    if not grounded:
        return DocumentAnalysisResult(schema_version=request.document_analysis_schema_version, organization_id=request.organization_id, document_id=request.document_id, preprocessing_run_id=request.preprocessing_run_id, preprocessing_schema_version=request.preprocessing_schema_version, prompt_version=request.prompt_version, agent_version=request.agent_version)
    evidence = EvidenceReference(preprocessing_run_id=request.preprocessing_run_id, page_number=1, block_id="native:p0001:b000000", representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE)
    return DocumentAnalysisResult(schema_version=request.document_analysis_schema_version, organization_id=request.organization_id, document_id=request.document_id, preprocessing_run_id=request.preprocessing_run_id, preprocessing_schema_version=request.preprocessing_schema_version, prompt_version=request.prompt_version, agent_version=request.agent_version, product_candidates=(ProductCandidate(candidate_id="candidate-1", raw_name="Generated", evidence=(evidence,)),), evidence_references=(evidence,))


def _codes(request, result, runtime):
    """Return only safe completion issue codes."""
    return {issue.code for issue in validate_document_analysis_result(request, result, runtime)}


def test_zero_or_failed_tool_calls_cannot_complete():
    """Failed calls are absent from the successful trace and cannot satisfy grounding."""
    request = _request()
    assert {"overview_not_retrieved", "document_content_not_retrieved"} <= _codes(request, _result(request), _runtime())


def test_overview_and_content_are_both_required():
    """Neither overview alone nor content alone makes a result complete."""
    request = _request()
    assert "document_content_not_retrieved" in _codes(request, _result(request), _runtime(get_document_overview=1))
    assert "overview_not_retrieved" in _codes(request, _result(request), _runtime(search_document=1))
    assert not _codes(request, _result(request), _runtime(get_document_overview=1, get_page_text=1))


def test_empty_and_ungrounded_material_results_are_rejected():
    """Require semantic findings and persisted evidence independently of tool grounding."""
    request = _request()
    trace = _runtime(get_document_overview=1, search_document=1)
    assert "empty_document_analysis" in _codes(request, _result(request, grounded=False), trace)
    evidence_free = _result(request).model_copy(update={"product_candidates": (ProductCandidate(candidate_id="candidate-1", raw_name="Generated"),)})
    assert "ungrounded_material_conclusion" in _codes(request, evidence_free, trace)
