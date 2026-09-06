"""Test the SKU-mapping task contract without database, catalog, or agent work."""

import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from maximor.document_analysis.schemas import (
    CommercialStatus,
    CommercialStatusAssessment,
    DocumentAnalysisResult,
    EvidenceReference,
    EvidenceRepresentation,
    ProductCandidate,
)
from maximor.preprocessing.schemas import ExtractionSource
from maximor.sku_mapping.contracts import SkuMappingTask, build_sku_mapping_task
from maximor.sku_mapping.errors import SkuMappingTaskConstructionError
from maximor.sku_mapping.versions import SKU_MAPPING_TASK_SCHEMA_VERSION


def _evidence(preprocessing_run_id: uuid.UUID, block_id: str = "native:p0001:b000000") -> EvidenceReference:
    return EvidenceReference(
        preprocessing_run_id=preprocessing_run_id,
        page_number=1,
        block_id=block_id,
        representation=EvidenceRepresentation.NATIVE_TEXT,
        extraction_source=ExtractionSource.NATIVE,
    )


def _result(preprocessing_run_id: uuid.UUID, *, statuses: tuple[CommercialStatusAssessment, ...]) -> DocumentAnalysisResult:
    candidate = ProductCandidate(
        candidate_id="candidate-0001",
        raw_name="Talent Acquisition",
        raw_attributes={"sku_description": "Talent Acquisition module"},
        evidence=(_evidence(preprocessing_run_id),),
    )
    return DocumentAnalysisResult(
        schema_version="analysis-v1",
        organization_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        preprocessing_run_id=preprocessing_run_id,
        preprocessing_schema_version="prep-v1",
        prompt_version="prompt-v1",
        agent_version="agent-v1",
        product_candidates=(candidate,),
        commercial_statuses=statuses,
    )


def test_task_requires_at_least_one_evidence_reference():
    """Reject a task grounded in neither candidate nor commercial-status evidence."""

    with pytest.raises(ValidationError):
        SkuMappingTask(
            schema_version=SKU_MAPPING_TASK_SCHEMA_VERSION,
            organization_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            preprocessing_run_id=uuid.uuid4(),
            analysis_run_id=uuid.uuid4(),
            document_analysis_schema_version="analysis-v1",
            document_analysis_agent_version="agent-v1",
            candidate_id="candidate-0001",
            raw_name="Talent Acquisition",
            commercial_status=CommercialStatus.PURCHASED,
        )


def test_task_rejects_evidence_from_another_preprocessing_run():
    """Reject a task whose evidence does not belong to its own preprocessing run."""

    preprocessing_run_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        SkuMappingTask(
            schema_version=SKU_MAPPING_TASK_SCHEMA_VERSION,
            organization_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            preprocessing_run_id=preprocessing_run_id,
            analysis_run_id=uuid.uuid4(),
            document_analysis_schema_version="analysis-v1",
            document_analysis_agent_version="agent-v1",
            candidate_id="candidate-0001",
            raw_name="Talent Acquisition",
            commercial_status=CommercialStatus.PURCHASED,
            candidate_evidence=(_evidence(uuid.uuid4()),),
        )


def test_task_keeps_candidate_and_status_evidence_separately_labelled():
    """Preserve distinct provenance while offering a deduplicated convenience view."""

    preprocessing_run_id = uuid.uuid4()
    shared = _evidence(preprocessing_run_id, block_id="native:p0001:b000000")
    candidate_only = _evidence(preprocessing_run_id, block_id="native:p0001:b000001")
    task = SkuMappingTask(
        schema_version=SKU_MAPPING_TASK_SCHEMA_VERSION,
        organization_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        preprocessing_run_id=preprocessing_run_id,
        analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="analysis-v1",
        document_analysis_agent_version="agent-v1",
        candidate_id="candidate-0001",
        raw_name="Talent Acquisition",
        commercial_status=CommercialStatus.PURCHASED,
        candidate_evidence=(candidate_only, shared),
        commercial_status_evidence=(shared,),
    )
    assert task.candidate_evidence == (candidate_only, shared)
    assert task.commercial_status_evidence == (shared,)
    # Deterministic order: candidate evidence first, then unseen status evidence; no duplicate.
    assert task.all_evidence == (candidate_only, shared)


def test_build_sku_mapping_task_from_one_validated_candidate():
    """Construct a task from a candidate and its exactly-one linked commercial status."""

    preprocessing_run_id = uuid.uuid4()
    status_evidence = _evidence(preprocessing_run_id, block_id="native:p0001:b000002")
    status = CommercialStatusAssessment(
        assessment_id="status-0001",
        status=CommercialStatus.OPTIONAL,
        candidate_id="candidate-0001",
        raw_rationale="Listed as an optional add-on.",
        evidence=(status_evidence,),
    )
    result = _result(preprocessing_run_id, statuses=(status,))
    analysis_run_id = uuid.uuid4()

    task = build_sku_mapping_task(
        result=result,
        analysis_run_id=analysis_run_id,
        candidate_id="candidate-0001",
        schema_version=SKU_MAPPING_TASK_SCHEMA_VERSION,
    )

    assert task.organization_id == result.organization_id
    assert task.document_id == result.document_id
    assert task.analysis_run_id == analysis_run_id
    assert task.document_analysis_schema_version == result.schema_version
    assert task.document_analysis_agent_version == result.agent_version
    assert task.raw_name == "Talent Acquisition"
    assert task.raw_attributes == {"sku_description": "Talent Acquisition module"}
    # Eligibility (whether OPTIONAL should be mapped at all) is explicitly not decided here.
    assert task.commercial_status == CommercialStatus.OPTIONAL
    assert task.commercial_status_rationale == "Listed as an optional add-on."
    assert task.commercial_status_evidence == (status_evidence,)


def test_build_sku_mapping_task_rejects_unknown_candidate():
    """Fail construction explicitly when the candidate id does not exist in the result."""

    preprocessing_run_id = uuid.uuid4()
    status = CommercialStatusAssessment(
        assessment_id="status-0001", status=CommercialStatus.PURCHASED, candidate_id="candidate-0001",
        evidence=(_evidence(preprocessing_run_id),),
    )
    result = _result(preprocessing_run_id, statuses=(status,))
    with pytest.raises(SkuMappingTaskConstructionError) as excinfo:
        build_sku_mapping_task(
            result=result, analysis_run_id=uuid.uuid4(), candidate_id="candidate-9999",
            schema_version=SKU_MAPPING_TASK_SCHEMA_VERSION,
        )
    assert excinfo.value.code == "sku_mapping_candidate_not_found"


def test_build_sku_mapping_task_rejects_missing_commercial_status():
    """Fail construction explicitly when the candidate has no linked status assessment."""

    preprocessing_run_id = uuid.uuid4()
    result = _result(preprocessing_run_id, statuses=())
    with pytest.raises(SkuMappingTaskConstructionError) as excinfo:
        build_sku_mapping_task(
            result=result, analysis_run_id=uuid.uuid4(), candidate_id="candidate-0001",
            schema_version=SKU_MAPPING_TASK_SCHEMA_VERSION,
        )
    assert excinfo.value.code == "sku_mapping_status_missing"


def test_build_sku_mapping_task_rejects_ambiguous_multiple_statuses():
    """Fail construction explicitly when more than one status links to the candidate.

    `DocumentAnalysisResult`'s own identifier-uniqueness validator already
    prevents two `commercial_statuses` entries from sharing one `candidate_id`
    in practice (its generic id lookup resolves to `candidate_id` before
    `assessment_id` for this model), so a real `DocumentAnalysisResult` can
    never reach `build_sku_mapping_task` in that state. This test exercises
    `build_sku_mapping_task`'s own defensive check directly, using a minimal
    stand-in that exposes only the attributes the function actually reads,
    instead of working around the existing document-analysis validator.
    """

    preprocessing_run_id = uuid.uuid4()
    candidate = ProductCandidate(
        candidate_id="candidate-0001", raw_name="Talent Acquisition",
        evidence=(_evidence(preprocessing_run_id),),
    )
    first = CommercialStatusAssessment(
        assessment_id="status-0001", status=CommercialStatus.PURCHASED, candidate_id="candidate-0001",
        evidence=(_evidence(preprocessing_run_id),),
    )
    second = CommercialStatusAssessment(
        assessment_id="status-0002", status=CommercialStatus.AMBIGUOUS, candidate_id="candidate-0001",
        evidence=(_evidence(preprocessing_run_id),),
    )
    result = SimpleNamespace(
        organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=preprocessing_run_id,
        schema_version="analysis-v1", agent_version="agent-v1",
        product_candidates=(candidate,), commercial_statuses=(first, second),
    )
    with pytest.raises(SkuMappingTaskConstructionError) as excinfo:
        build_sku_mapping_task(
            result=result, analysis_run_id=uuid.uuid4(), candidate_id="candidate-0001",
            schema_version=SKU_MAPPING_TASK_SCHEMA_VERSION,
        )
    assert excinfo.value.code == "sku_mapping_status_ambiguous"
