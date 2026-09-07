"""Test the term-triage task/result contracts without database or agent work."""

import uuid

import pytest
from pydantic import ValidationError

from maximor.document_analysis.schemas import (
    CommercialStatus,
    CommercialStatusAssessment,
    DocumentAnalysisResult,
    EvidenceReference,
    EvidenceRepresentation,
    GlobalTerm,
    ProductCandidate,
)
from maximor.preprocessing.schemas import ExtractionSource
from maximor.term_triage.contracts import (
    TermTriageTask,
    TriageCandidateContext,
    TriageTermContext,
    build_term_triage_result,
    build_term_triage_task,
)
from maximor.term_triage.errors import TermTriageResultConstructionError, TermTriageTaskConstructionError
from maximor.term_triage.schemas import TermTriageDecision, TermTriageDisposition, TermTriageResult


def _evidence(run_id: uuid.UUID) -> EvidenceReference:
    return EvidenceReference(
        preprocessing_run_id=run_id, page_number=1, block_id="native:p0001:b000000",
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    )


def _result(run_id: uuid.UUID, *, terms=()) -> DocumentAnalysisResult:
    candidate = ProductCandidate(candidate_id="candidate-0001", raw_name="X", evidence=(_evidence(run_id),))
    status = CommercialStatusAssessment(assessment_id="status-0001", status=CommercialStatus.PURCHASED, candidate_id="candidate-0001", evidence=(_evidence(run_id),))
    return DocumentAnalysisResult(
        schema_version="analysis-v1", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=run_id, preprocessing_schema_version="prep-v1",
        prompt_version="p1", agent_version="a1",
        product_candidates=(candidate,), commercial_statuses=(status,), global_terms=terms,
    )


# --- TermTriageDecision cannot express applicability or evidence -----------------

def test_decision_has_no_applicability_or_candidate_or_evidence_fields():
    decision = TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.UNCERTAIN)
    assert not hasattr(decision, "applicability_scope")
    assert not hasattr(decision, "applies_to_candidate_ids")
    assert not hasattr(decision, "evidence")
    with pytest.raises(ValidationError):
        TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.UNCERTAIN, applicability_scope="document")
    with pytest.raises(ValidationError):
        TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.UNCERTAIN, applies_to_candidate_ids=["candidate-0001"])
    with pytest.raises(ValidationError):
        TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.UNCERTAIN, evidence=[])
    with pytest.raises(ValidationError):
        TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.UNCERTAIN, evidence_ids=["native:p0001:b000000"])


def test_decision_and_result_are_frozen_and_forbid_extra():
    decision = TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.DOCUMENT_METADATA)
    with pytest.raises(ValidationError):
        decision.term_id = "term-9999"


def test_rationale_is_bounded():
    with pytest.raises(ValidationError):
        TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.DOCUMENT_METADATA, rationale="x" * 301)


# --- build_term_triage_task -----------------------------------------------------

def test_build_term_triage_task_from_valid_result():
    run_id = uuid.uuid4()
    term = GlobalTerm(term_id="term-0001", raw_name="Payment terms", raw_value="Net 45", evidence=(_evidence(run_id),))
    result = _result(run_id, terms=(term,))
    task = build_term_triage_task(result=result, analysis_run_id=uuid.uuid4(), schema_version="1.0.0")
    assert task.term_ids == ("term-0001",)
    assert task.terms[0].raw_value == "Net 45"
    assert task.candidates[0].commercial_status == CommercialStatus.PURCHASED
    # No evidence of any kind belongs on a triage term/candidate context.
    assert not hasattr(task.terms[0], "evidence_ids")
    assert not hasattr(task.candidates[0], "evidence_ids")


def test_build_term_triage_task_rejects_candidate_without_status():
    run_id = uuid.uuid4()
    candidate = ProductCandidate(candidate_id="candidate-0001", raw_name="X", evidence=(_evidence(run_id),))
    result = DocumentAnalysisResult(
        schema_version="analysis-v1", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=run_id, preprocessing_schema_version="prep-v1",
        prompt_version="p", agent_version="a", product_candidates=(candidate,),
    )
    with pytest.raises(TermTriageTaskConstructionError) as captured:
        build_term_triage_task(result=result, analysis_run_id=uuid.uuid4(), schema_version="1.0.0")
    assert captured.value.code == "term_triage_status_missing"


def test_task_rejects_duplicate_or_unordered_terms():
    with pytest.raises(ValidationError):
        TermTriageTask(
            schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
            preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
            document_analysis_schema_version="a", document_analysis_agent_version="a",
            terms=(TriageTermContext(term_id="term-0001", raw_name="A"), TriageTermContext(term_id="term-0001", raw_name="B")),
        )


# --- build_term_triage_result: every term gets exactly one decision ------------

def _task(term_ids=("term-0001", "term-0002")) -> TermTriageTask:
    return TermTriageTask(
        schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="a", document_analysis_agent_version="a",
        terms=tuple(TriageTermContext(term_id=tid, raw_name="X") for tid in term_ids),
    )


def test_build_term_triage_result_requires_exactly_one_decision_per_term():
    task = _task()
    decisions = (
        TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.DOCUMENT_METADATA),
        TermTriageDecision(term_id="term-0002", disposition=TermTriageDisposition.POTENTIAL_LINE_ITEM),
    )
    result = build_term_triage_result(task=task, decisions=decisions, schema_version="1.0.0")
    assert len(result.decisions) == 2


def test_build_term_triage_result_rejects_missing_coverage():
    task = _task()
    decisions = (TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.DOCUMENT_METADATA),)
    with pytest.raises(TermTriageResultConstructionError) as captured:
        build_term_triage_result(task=task, decisions=decisions, schema_version="1.0.0")
    assert captured.value.code == "term_triage_incomplete_coverage"


def test_build_term_triage_result_rejects_unknown_term():
    task = _task(term_ids=("term-0001",))
    decisions = (
        TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.DOCUMENT_METADATA),
        TermTriageDecision(term_id="term-missing", disposition=TermTriageDisposition.UNCERTAIN),
    )
    with pytest.raises(TermTriageResultConstructionError) as captured:
        build_term_triage_result(task=task, decisions=decisions, schema_version="1.0.0")
    assert captured.value.code == "term_triage_unknown_term"


def test_result_rejects_duplicate_or_unordered_decisions():
    with pytest.raises(ValidationError):
        TermTriageResult(
            schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
            preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
            decisions=(
                TermTriageDecision(term_id="term-0002", disposition=TermTriageDisposition.DOCUMENT_METADATA),
                TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.DOCUMENT_METADATA),
            ),
        )
