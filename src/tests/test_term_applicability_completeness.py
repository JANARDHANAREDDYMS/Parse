"""Focused completeness tests for expanded term-applicability results."""

import uuid

from maximor.document_analysis.schemas import CommercialStatus, EvidenceReference, EvidenceRepresentation
from maximor.preprocessing.schemas import ExtractionSource
from maximor.term_applicability.contracts import CandidateContext, TermApplicabilityTask, TermContext
from maximor.term_applicability.agent import ClaudeTermApplicabilityAgent
from maximor.term_applicability.schemas import (
    CandidateCommercialFactCoverage, CandidateCommercialFacts, RawCommercialFact,
    RawCommercialFactField, TermApplicabilityDecision, TermApplicabilityResult,
    TermDisposition, compute_fact_id,
)
from maximor.term_applicability.validation import validate_term_applicability_result
from maximor.term_applicability.versions import TERM_APPLICABILITY_RESULT_SCHEMA_VERSION


class Runtime:
    """Supply only successful candidate-evidence facts to deterministic validation."""

    retrieved_term_evidence_ids = {}
    retrieved_candidate_evidence_ids = {"candidate-0001": frozenset({"native:p0001:b000001"})}


def task(*, raw_attributes=None, status=CommercialStatus.PURCHASED, selected=True):
    """Build an in-memory task with one candidate and one selected term."""

    run = uuid.uuid4()
    evidence = EvidenceReference(
        preprocessing_run_id=run, page_number=1, block_id="native:p0001:b000001",
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    )
    return TermApplicabilityTask(
        schema_version="task-v1", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=run, analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="analysis-v1", document_analysis_agent_version="agent-v1",
        candidates=(CandidateContext(candidate_id="candidate-0001", raw_name="Service", commercial_status=status, raw_attributes=raw_attributes or {}, evidence_ids=("native:p0001:b000001",)),),
        terms=(TermContext(term_id="term-0001", raw_name="Payment", raw_value="Net 30"),),
        evidence_by_id={"native:p0001:b000001": evidence},
        selected_term_ids=("term-0001",) if selected else None,
    )


def result(task, *, decisions=(), facts=(), coverage=()):
    """Build a result tied to the supplied task."""

    return TermApplicabilityResult(
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION,
        organization_id=task.organization_id, document_id=task.document_id,
        preprocessing_run_id=task.preprocessing_run_id, analysis_run_id=task.analysis_run_id,
        decisions=decisions, candidate_commercial_facts=facts,
        candidate_commercial_fact_coverage=coverage,
    )


def test_selected_term_omission_is_rejected():
    t = task()
    issues = validate_term_applicability_result(t, result(t), Runtime())
    assert any(issue.code == "selected_term_decision_missing" for issue in issues)


def test_eligible_raw_hints_require_coverage():
    t = task(raw_attributes={"qty": "2", "unit_price": "$10", "total_contract_value": "$20"})
    decision = TermApplicabilityDecision(schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.DOCUMENT_METADATA)
    issues = validate_term_applicability_result(t, result(t, decisions=(decision,)), Runtime())
    assert any(issue.code == "eligible_candidate_fact_coverage_missing" for issue in issues)


def test_extracted_fact_and_unresolved_coverage_partition_expected_fields():
    t = task(raw_attributes={"qty": "2", "unit_price": "$10", "total_contract_value": "$20"})
    evidence = t.evidence_by_id["native:p0001:b000001"]
    fact = RawCommercialFact(
        fact_id=compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.QUANTITY, raw_value="2"),
        candidate_id="candidate-0001", field=RawCommercialFactField.QUANTITY, raw_value="2", evidence=(evidence,),
    )
    bundle = CandidateCommercialFacts(candidate_id="candidate-0001", facts=(fact,))
    coverage = CandidateCommercialFactCoverage(
        candidate_id="candidate-0001",
        expected_fields=(
            RawCommercialFactField.INVOICING_FREQUENCY, RawCommercialFactField.INVOICING_SCHEDULE_TYPE,
            RawCommercialFactField.PAYMENT_TERMS, RawCommercialFactField.QUANTITY,
            RawCommercialFactField.SERVICE_END_DATE, RawCommercialFactField.SERVICE_START_DATE,
            RawCommercialFactField.TOTAL_LISTED_VALUE, RawCommercialFactField.UNIT_PRICE,
        ),
        extracted_fields=(RawCommercialFactField.QUANTITY,),
        unresolved_fields=(
            RawCommercialFactField.INVOICING_FREQUENCY, RawCommercialFactField.INVOICING_SCHEDULE_TYPE,
            RawCommercialFactField.PAYMENT_TERMS, RawCommercialFactField.SERVICE_END_DATE,
            RawCommercialFactField.SERVICE_START_DATE, RawCommercialFactField.TOTAL_LISTED_VALUE,
            RawCommercialFactField.UNIT_PRICE,
        ),
        evidence=(evidence,),
    )
    decision = TermApplicabilityDecision(schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.DOCUMENT_METADATA)
    assert validate_term_applicability_result(t, result(t, decisions=(decision,), facts=(bundle,), coverage=(coverage,)), Runtime()) == ()


def test_excluded_candidate_cannot_receive_coverage():
    t = task(raw_attributes={"qty": "2"}, status=CommercialStatus.EXCLUDED)
    coverage = CandidateCommercialFactCoverage(candidate_id="candidate-0001", expected_fields=(), extracted_fields=(), unresolved_fields=())
    issues = validate_term_applicability_result(t, result(t, coverage=(coverage,)), Runtime())
    assert any(issue.code == "candidate_fact_coverage_candidate_not_eligible" for issue in issues)


def test_old_term_only_result_remains_loadable_with_empty_coverage():
    old = TermApplicabilityResult.model_validate({
        "schema_version": "1.0.0", "organization_id": str(uuid.uuid4()),
        "document_id": str(uuid.uuid4()), "preprocessing_run_id": str(uuid.uuid4()),
        "analysis_run_id": str(uuid.uuid4()), "decisions": [],
    })
    assert old.candidate_commercial_facts == ()
    assert old.candidate_commercial_fact_coverage == ()


def test_finalizer_coverage_resolution_injects_expected_fields_and_evidence():
    """The application, not Claude, resolves coverage evidence, expected fields, and unresolved-only coverage."""

    t = task(raw_attributes={"qty": "2"})
    resolved = ClaudeTermApplicabilityAgent._resolve_coverage_evidence_ids(t, [{
        "candidate_id": "candidate-0001",
        "unresolved_fields": ["quantity"], "evidence_ids": ["native:p0001:b000001"],
    }], [])
    assert resolved and resolved[0]["expected_fields"] == [
        "invoicing_frequency", "invoicing_schedule_type", "payment_terms",
        "quantity", "service_end_date", "service_start_date",
    ]
    assert resolved[0]["extracted_fields"] == []
    assert resolved[0]["evidence"][0]["block_id"] == "native:p0001:b000001"


def test_finalizer_coverage_resolution_computes_extracted_fields_from_resolved_facts():
    """`extracted_fields` is derived from this same submission's own resolved facts, never from Claude."""

    t = task(raw_attributes={"qty": "2"})
    resolved_facts = [{"candidate_id": "candidate-0001", "facts": [{"field": "quantity"}]}]
    resolved = ClaudeTermApplicabilityAgent._resolve_coverage_evidence_ids(t, [{
        "candidate_id": "candidate-0001",
        "unresolved_fields": [], "evidence_ids": ["native:p0001:b000001"],
    }], resolved_facts)
    assert resolved and resolved[0]["extracted_fields"] == ["quantity"]


def test_finalizer_coverage_resolution_rejects_a_submission_that_still_includes_extracted_fields():
    """A coverage item that still restates `extracted_fields` is rejected, never silently accepted."""

    t = task(raw_attributes={"qty": "2"})
    resolved = ClaudeTermApplicabilityAgent._resolve_coverage_evidence_ids(t, [{
        "candidate_id": "candidate-0001", "extracted_fields": ["quantity"],
        "unresolved_fields": [], "evidence_ids": ["native:p0001:b000001"],
    }], [])
    assert resolved is None
