"""Test identifier-only document-analysis contracts without PDF, database, or agent work."""

import uuid

import pytest
from pydantic import ValidationError

from maximor.document_analysis.agent import UnconfiguredDocumentAnalysisAgent
from maximor.document_analysis.contracts import DocumentAnalysisRequest
from maximor.document_analysis.errors import DocumentAnalysisNotConfiguredError
from maximor.document_analysis.schemas import (
    CommercialStatus,
    CommercialStatusAssessment,
    DocumentAnalysisResult,
    ApplicabilityScope,
    EvidenceReference,
    EvidenceRepresentation,
    ProductCandidate,
    GlobalTerm,
    MAX_PRODUCT_CANDIDATES,
    MAX_EVIDENCE_PER_ENTITY,
)
from maximor.preprocessing.schemas import ExtractionSource


def evidence() -> EvidenceReference:
    """Return one stable native-text evidence reference for contract tests."""

    return EvidenceReference(
        preprocessing_run_id=uuid.uuid4(),
        page_number=1,
        block_id="native:p0001:b000000",
        representation=EvidenceRepresentation.NATIVE_TEXT,
        extraction_source=ExtractionSource.NATIVE,
    )


def test_request_is_identifier_only_and_versioned():
    """Accept a compact request with no preprocessing document payload."""

    request = DocumentAnalysisRequest(
        organization_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        preprocessing_run_id=uuid.uuid4(),
        preprocessing_schema_version="1.0.0",
        document_analysis_schema_version="1.0.0",
        prompt_version="prompt-1", skill_version="skill-1",
        agent_version="agent-1",
    )
    assert request.preprocessing_schema_version == "1.0.0"
    with pytest.raises(ValidationError):
        DocumentAnalysisRequest(**request.model_dump(), preprocessed_document={})


def test_evidence_requires_one_compatible_persisted_target():
    """Reject evidence without a target, with two targets, or with wrong representation."""

    assert evidence().block_id
    values = evidence().model_dump()
    with pytest.raises(ValidationError):
        EvidenceReference(**{**values, "block_id": None})
    with pytest.raises(ValidationError):
        EvidenceReference(**{**values, "table_id": "table:p0001:t0000"})
    with pytest.raises(ValidationError):
        EvidenceReference(**{**values, "representation": EvidenceRepresentation.TABLE})


def test_result_rejects_evidence_from_another_preprocessing_run():
    """Prevent an analysis result from mixing persisted tenant-run evidence."""

    run_id = uuid.uuid4()
    candidate = ProductCandidate(
        candidate_id="candidate:000001",
        raw_name="Service",
        evidence=(evidence(),),
    )
    with pytest.raises(ValidationError):
        DocumentAnalysisResult(
            schema_version="1", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
            preprocessing_run_id=run_id, preprocessing_schema_version="1",
            prompt_version="p", agent_version="a", product_candidates=(candidate,),
        )


def test_product_candidate_rejects_final_sku_mapping_fields():
    """Keep candidate detection distinct from later authoritative SKU mapping."""

    candidate = ProductCandidate(
        candidate_id="candidate:000001",
        raw_name="Raw service name",
        raw_attributes={"raw_price": "$10"},
        evidence=(evidence(),),
    )
    assert candidate.raw_name == "Raw service name"
    with pytest.raises(ValidationError):
        ProductCandidate(
            candidate_id="candidate:000002",
            raw_name="Raw service name",
            raw_attributes={"sku_code": "NOT-ALLOWED"},
        )
    with pytest.raises(ValidationError):
        ProductCandidate(
            candidate_id="candidate:000003",
            raw_name="Raw service name",
            final_sku="NOT-ALLOWED",
        )


def test_result_enforces_candidate_status_links_and_deterministic_order():
    """Accept precise commercial status and reject an unknown candidate reference."""

    run_id = uuid.uuid4()
    candidate = ProductCandidate(candidate_id="candidate:000001", raw_name="Service")
    result = DocumentAnalysisResult(
        schema_version="1.0.0",
        organization_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        preprocessing_run_id=run_id,
        preprocessing_schema_version="1.0.0",
        prompt_version="prompt-1",
        agent_version="agent-1",
        product_candidates=(candidate,),
        commercial_statuses=(
            CommercialStatusAssessment(
                assessment_id="status:000001",
                candidate_id=candidate.candidate_id,
                status=CommercialStatus.PURCHASED,
            ),
        ),
    )
    assert result.commercial_statuses[0].status == CommercialStatus.PURCHASED
    invalid_values = result.model_dump()
    invalid_values["commercial_statuses"] = (
        CommercialStatusAssessment(
            assessment_id="status:000001",
            candidate_id="candidate:missing",
            status=CommercialStatus.AMBIGUOUS,
        ),
    )
    with pytest.raises(ValidationError):
        DocumentAnalysisResult(**invalid_values)


def test_global_term_applicability_scopes_are_explicit_and_bounded():
    """Preserve document, candidate, and unknown applicability without normalization."""

    candidate = ProductCandidate(candidate_id="candidate:000001", raw_name="Service")
    base = dict(term_id="term:000001", raw_name="Payment terms")
    assert GlobalTerm(**base).applicability_scope == ApplicabilityScope.UNKNOWN
    assert GlobalTerm(**base, applicability_scope=ApplicabilityScope.DOCUMENT).applies_to_candidate_ids == ()
    scoped = GlobalTerm(**base, applicability_scope=ApplicabilityScope.CANDIDATE, applies_to_candidate_ids=(candidate.candidate_id,))
    run_id = uuid.uuid4()
    result = DocumentAnalysisResult(
        schema_version="1", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=run_id, preprocessing_schema_version="1", prompt_version="p", agent_version="a",
        product_candidates=(candidate,), global_terms=(scoped,),
    )
    assert result.global_terms[0].applicability_scope == ApplicabilityScope.CANDIDATE
    with pytest.raises(ValidationError):
        GlobalTerm(**base, applicability_scope=ApplicabilityScope.CANDIDATE)
    with pytest.raises(ValidationError):
        GlobalTerm(**base, applicability_scope=ApplicabilityScope.UNKNOWN, applies_to_candidate_ids=(candidate.candidate_id,))
    with pytest.raises(ValidationError):
        GlobalTerm(**base, applicability_scope=ApplicabilityScope.CANDIDATE, applies_to_candidate_ids=("candidate:000002", candidate.candidate_id))
    with pytest.raises(ValidationError):
        DocumentAnalysisResult(
            schema_version="1", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
            preprocessing_run_id=run_id, preprocessing_schema_version="1", prompt_version="p", agent_version="a",
            global_terms=(GlobalTerm(**base, applicability_scope=ApplicabilityScope.CANDIDATE, applies_to_candidate_ids=("candidate:missing",)),),
        )


@pytest.mark.asyncio
async def test_placeholder_agent_fails_explicitly():
    """Ensure the skeleton never returns a fabricated successful analysis."""

    request = DocumentAnalysisRequest(
        organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=uuid.uuid4(), preprocessing_schema_version="1",
        document_analysis_schema_version="1", prompt_version="p", skill_version="s", agent_version="a",
    )
    with pytest.raises(DocumentAnalysisNotConfiguredError) as captured:
        await UnconfiguredDocumentAnalysisAgent().analyze(request, object())
    assert captured.value.code == "document_analysis_not_configured"


def test_result_collections_text_and_evidence_are_bounded():
    """Reject repetitive semantic output before it can consume unbounded storage or tokens."""

    run_id = uuid.uuid4()
    base = dict(
        schema_version="1", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=run_id, preprocessing_schema_version="1",
        prompt_version="p", agent_version="a",
    )
    with pytest.raises(ValidationError):
        DocumentAnalysisResult(**base, product_candidates=tuple(
            ProductCandidate(candidate_id=f"candidate:{index:04d}", raw_name="Service")
            for index in range(MAX_PRODUCT_CANDIDATES + 1)
        ))
    item_evidence = EvidenceReference(
        preprocessing_run_id=run_id, page_number=1,
        block_id="native:p0001:b000000",
        representation=EvidenceRepresentation.NATIVE_TEXT,
    )
    with pytest.raises(ValidationError):
        ProductCandidate(
            candidate_id="candidate:bounded", raw_name="Service",
            evidence=(item_evidence,) * (MAX_EVIDENCE_PER_ENTITY + 1),
        )
    with pytest.raises(ValidationError):
        ProductCandidate(candidate_id="candidate:text", raw_name="x" * 2_001)
    with pytest.raises(ValidationError):
        ProductCandidate(
            candidate_id="candidate:attributes", raw_name="Service",
            raw_attributes={f"field-{index}": "value" for index in range(26)},
        )
