"""Test the deterministic term-applicability completion gate.

No database, no Claude call.
"""

import uuid

from maximor.document_analysis.schemas import ApplicabilityScope, CommercialStatus, EvidenceReference, EvidenceRepresentation
from maximor.preprocessing.schemas import ExtractionSource
from maximor.term_applicability.contracts import CandidateContext, TermApplicabilityTask, TermContext
from maximor.term_applicability.schemas import (
    CandidateCommercialFacts,
    RawCommercialFact,
    RawCommercialFactField,
    TermApplicabilityDecision,
    TermApplicabilityResult,
    TermDisposition,
    compute_fact_id,
)
from maximor.term_applicability.validation import validate_term_applicability_result
from maximor.term_applicability.versions import TERM_APPLICABILITY_RESULT_SCHEMA_VERSION


def _evidence(run_id: uuid.UUID, block_id: str = "native:p0001:b000000") -> EvidenceReference:
    return EvidenceReference(
        preprocessing_run_id=run_id, page_number=1, block_id=block_id,
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    )


def _task() -> TermApplicabilityTask:
    run_id = uuid.uuid4()
    term_evidence = _evidence(run_id, "native:p0001:b000000")
    candidate_evidence = _evidence(run_id, "native:p0001:b000001")
    return TermApplicabilityTask(
        schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=run_id, analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="a", document_analysis_agent_version="a",
        candidates=(CandidateContext(candidate_id="candidate-0001", raw_name="X", commercial_status=CommercialStatus.PURCHASED, evidence_ids=("native:p0001:b000001",)),),
        terms=(TermContext(term_id="term-0001", raw_name="Y", evidence_ids=("native:p0001:b000000",)),),
        evidence_by_id={"native:p0001:b000000": term_evidence, "native:p0001:b000001": candidate_evidence},
    )


def _result(
    task: TermApplicabilityTask, decisions: tuple,
    candidate_commercial_facts: tuple = (),
) -> TermApplicabilityResult:
    return TermApplicabilityResult(
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, organization_id=task.organization_id,
        document_id=task.document_id, preprocessing_run_id=task.preprocessing_run_id,
        analysis_run_id=task.analysis_run_id, decisions=decisions,
        candidate_commercial_facts=candidate_commercial_facts,
    )


class _Runtime:
    def __init__(self, retrieved_term=None, retrieved_candidate=None):
        self.retrieved_term_evidence_ids = retrieved_term or {}
        self.retrieved_candidate_evidence_ids = retrieved_candidate or {}


def test_valid_document_scope_decision_with_grounded_retrieval_passes():
    task = _task()
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(task.evidence_by_id["native:p0001:b000000"],),
    )
    runtime = _Runtime(retrieved_term={"term-0001": frozenset({"native:p0001:b000000"})})
    issues = validate_term_applicability_result(task, _result(task, (decision,)), runtime)
    assert issues == ()


def test_valid_unknown_scope_decision_with_grounded_retrieval_passes():
    task = _task()
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.UNKNOWN, evidence=(task.evidence_by_id["native:p0001:b000000"],),
    )
    runtime = _Runtime(retrieved_term={"term-0001": frozenset({"native:p0001:b000000"})})
    issues = validate_term_applicability_result(task, _result(task, (decision,)), runtime)
    assert issues == ()


def test_valid_candidate_scope_decision_with_both_retrievals_passes():
    task = _task()
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.CANDIDATE, applies_to_candidate_ids=("candidate-0001",),
        evidence=(task.evidence_by_id["native:p0001:b000000"],),
    )
    runtime = _Runtime(
        retrieved_term={"term-0001": frozenset({"native:p0001:b000000"})},
        retrieved_candidate={"candidate-0001": frozenset({"native:p0001:b000001"})},
    )
    issues = validate_term_applicability_result(task, _result(task, (decision,)), runtime)
    assert issues == ()


def test_valid_document_metadata_decision_passes_without_any_retrieval():
    task = _task()
    decision = TermApplicabilityDecision(schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.DOCUMENT_METADATA)
    issues = validate_term_applicability_result(task, _result(task, (decision,)), _Runtime())
    assert issues == ()


def test_unknown_term_reference_is_rejected():
    task = _task()
    decision = TermApplicabilityDecision(schema_version="1.0.0", term_id="term-missing", disposition=TermDisposition.DOCUMENT_METADATA)
    issues = validate_term_applicability_result(task, _result(task, (decision,)), _Runtime())
    assert any(issue.code == "unknown_term" for issue in issues)


def test_unsupported_candidate_attribution_is_rejected():
    """A candidate-scope decision naming an unknown candidate is rejected."""

    task = _task()
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.CANDIDATE, applies_to_candidate_ids=("candidate-missing",),
        evidence=(task.evidence_by_id["native:p0001:b000000"],),
    )
    runtime = _Runtime(retrieved_term={"term-0001": frozenset({"native:p0001:b000000"})})
    issues = validate_term_applicability_result(task, _result(task, (decision,)), runtime)
    assert any(issue.code == "unknown_candidate" and not issue.correctable for issue in issues)


def test_evidence_not_grounded_in_task_is_rejected():
    """Decision evidence that does not match the task's own trusted evidence is rejected."""

    task = _task()
    foreign_evidence = _evidence(uuid.uuid4(), "native:p0001:b000000")
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.UNKNOWN, evidence=(foreign_evidence,),
    )
    issues = validate_term_applicability_result(task, _result(task, (decision,)), _Runtime())
    assert any(issue.code == "evidence_not_grounded_in_task" and issue.correctable for issue in issues)


def test_missing_successful_evidence_retrieval_is_rejected():
    """A line-item decision citing evidence never confirmed by a successful retrieval is rejected."""

    task = _task()
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.UNKNOWN, evidence=(task.evidence_by_id["native:p0001:b000000"],),
    )
    issues = validate_term_applicability_result(task, _result(task, (decision,)), _Runtime())
    assert any(issue.code == "evidence_not_retrieved" and issue.correctable for issue in issues)


def test_candidate_scope_without_candidate_evidence_retrieval_is_rejected():
    """A candidate-scope decision missing a successful candidate-evidence retrieval is rejected."""

    task = _task()
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.CANDIDATE, applies_to_candidate_ids=("candidate-0001",),
        evidence=(task.evidence_by_id["native:p0001:b000000"],),
    )
    runtime = _Runtime(retrieved_term={"term-0001": frozenset({"native:p0001:b000000"})})
    issues = validate_term_applicability_result(task, _result(task, (decision,)), runtime)
    assert any(issue.code == "candidate_evidence_not_retrieved" and issue.correctable for issue in issues)


def test_identity_mismatch_is_rejected():
    task = _task()
    result = TermApplicabilityResult(
        schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=task.document_id,
        preprocessing_run_id=task.preprocessing_run_id, analysis_run_id=task.analysis_run_id,
    )
    issues = validate_term_applicability_result(task, result, _Runtime())
    assert any(issue.code == "identity_mismatch" and not issue.correctable for issue in issues)


def test_version_mismatch_is_rejected():
    task = _task()
    result = TermApplicabilityResult(
        schema_version="0.0.1", organization_id=task.organization_id, document_id=task.document_id,
        preprocessing_run_id=task.preprocessing_run_id, analysis_run_id=task.analysis_run_id,
    )
    issues = validate_term_applicability_result(task, result, _Runtime())
    assert any(issue.code == "version_mismatch" and not issue.correctable for issue in issues)


def test_no_runtime_provided_still_rejects_ungrounded_line_item_evidence():
    """Without a runtime, retrieval-based checks fall back to empty (nothing is ever confirmed)."""

    task = _task()
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.UNKNOWN, evidence=(task.evidence_by_id["native:p0001:b000000"],),
    )
    issues = validate_term_applicability_result(task, _result(task, (decision,)), None)
    assert any(issue.code == "evidence_not_retrieved" for issue in issues)


def test_decision_for_a_real_but_unselected_term_is_rejected():
    """A term that genuinely exists in the task but was not triage-selected is still rejected."""

    task = _task()
    unselected_task = task.__class__(**{**task.model_dump(), "selected_term_ids": ()})
    decision = TermApplicabilityDecision(schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.DOCUMENT_METADATA)
    issues = validate_term_applicability_result(unselected_task, _result(unselected_task, (decision,)), _Runtime())
    assert any(issue.code == "term_not_selected" and issue.correctable for issue in issues)
    assert not any(issue.code == "unknown_term" for issue in issues)


# --- candidate commercial facts ---------------------------------------------------

def _fact(
    *, candidate_id: str = "candidate-0001", field: RawCommercialFactField = RawCommercialFactField.QUANTITY,
    raw_value: str = "12", raw_period_label: str | None = None,
    evidence: tuple = (),
) -> RawCommercialFact:
    return RawCommercialFact(
        fact_id=compute_fact_id(candidate_id=candidate_id, field=field, raw_value=raw_value, raw_period_label=raw_period_label),
        candidate_id=candidate_id, field=field, raw_value=raw_value, raw_period_label=raw_period_label,
        evidence=evidence,
    )


def _fact_task() -> TermApplicabilityTask:
    run_id = uuid.uuid4()
    candidate_evidence = _evidence(run_id, "native:p0001:b000002")
    return TermApplicabilityTask(
        schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=run_id, analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="a", document_analysis_agent_version="a",
        candidates=(
            CandidateContext(candidate_id="candidate-0001", raw_name="X", commercial_status=CommercialStatus.PURCHASED, evidence_ids=("native:p0001:b000002",)),
            CandidateContext(candidate_id="candidate-0002", raw_name="Y", commercial_status=CommercialStatus.EXCLUDED, evidence_ids=("native:p0001:b000002",)),
        ),
        evidence_by_id={"native:p0001:b000002": candidate_evidence},
    )


def test_valid_commercial_fact_with_grounded_retrieval_passes():
    task = _fact_task()
    fact = _fact(evidence=(task.evidence_by_id["native:p0001:b000002"],))
    bundle = CandidateCommercialFacts(candidate_id="candidate-0001", facts=(fact,))
    runtime = _Runtime(retrieved_candidate={"candidate-0001": frozenset({"native:p0001:b000002"})})
    issues = validate_term_applicability_result(task, _result(task, (), (bundle,)), runtime)
    assert issues == ()


def test_multiple_yearly_price_facts_with_distinct_periods_pass():
    task = _fact_task()
    evidence = (task.evidence_by_id["native:p0001:b000002"],)
    year_one = _fact(field=RawCommercialFactField.YEARLY_PRICE, raw_value="$100", raw_period_label="Year 1", evidence=evidence)
    year_two = _fact(field=RawCommercialFactField.YEARLY_PRICE, raw_value="$110", raw_period_label="Year 2", evidence=evidence)
    year_three = _fact(field=RawCommercialFactField.YEARLY_PRICE, raw_value="$120", raw_period_label="Year 3", evidence=evidence)
    bundle = CandidateCommercialFacts(candidate_id="candidate-0001", facts=(year_one, year_two, year_three))
    runtime = _Runtime(retrieved_candidate={"candidate-0001": frozenset({"native:p0001:b000002"})})
    issues = validate_term_applicability_result(task, _result(task, (), (bundle,)), runtime)
    assert issues == ()


def test_facts_for_an_excluded_candidate_are_rejected():
    """A commercial fact for a non-eligible candidate status is rejected as correctable."""

    task = _fact_task()
    fact = _fact(candidate_id="candidate-0002", evidence=(task.evidence_by_id["native:p0001:b000002"],))
    bundle = CandidateCommercialFacts(candidate_id="candidate-0002", facts=(fact,))
    runtime = _Runtime(retrieved_candidate={"candidate-0002": frozenset({"native:p0001:b000002"})})
    issues = validate_term_applicability_result(task, _result(task, (), (bundle,)), runtime)
    assert any(issue.code == "fact_candidate_not_eligible" and issue.correctable for issue in issues)


def test_facts_for_an_unknown_candidate_are_rejected():
    task = _fact_task()
    fact = _fact(candidate_id="candidate-missing", evidence=(task.evidence_by_id["native:p0001:b000002"],))
    bundle = CandidateCommercialFacts(candidate_id="candidate-missing", facts=(fact,))
    issues = validate_term_applicability_result(task, _result(task, (), (bundle,)), _Runtime())
    assert any(issue.code == "fact_unknown_candidate" and not issue.correctable for issue in issues)


def test_duplicate_fact_id_across_bundles_is_rejected():
    """Two bundles that happen to carry the same fact_id are rejected."""

    task = _fact_task()
    evidence = (task.evidence_by_id["native:p0001:b000002"],)
    fact = _fact(evidence=evidence)
    bundle = CandidateCommercialFacts(candidate_id="candidate-0001", facts=(fact,))
    duplicated = TermApplicabilityResult.model_construct(
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, organization_id=task.organization_id,
        document_id=task.document_id, preprocessing_run_id=task.preprocessing_run_id,
        analysis_run_id=task.analysis_run_id, decisions=(), candidate_commercial_facts=(bundle, bundle),
    )
    runtime = _Runtime(retrieved_candidate={"candidate-0001": frozenset({"native:p0001:b000002"})})
    issues = validate_term_applicability_result(task, duplicated, runtime)
    assert any(issue.code == "duplicate_fact_id" and issue.correctable for issue in issues)


def test_duplicate_same_field_and_period_facts_in_one_bundle_is_rejected():
    """Two facts on the same field with no distinguishing period context are rejected.

    Both facts also collide on `fact_id` here (quantity's canonical ID does not
    depend on `raw_value`), which is expected -- the period-context collision
    and the ID collision are two views of the same underlying problem.
    """

    task = _fact_task()
    evidence = (task.evidence_by_id["native:p0001:b000002"],)
    first = _fact(raw_value="10", evidence=evidence)
    second = _fact(raw_value="20", evidence=evidence)
    bundle = CandidateCommercialFacts.model_construct(candidate_id="candidate-0001", facts=(first, second))
    result = TermApplicabilityResult.model_construct(
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, organization_id=task.organization_id,
        document_id=task.document_id, preprocessing_run_id=task.preprocessing_run_id,
        analysis_run_id=task.analysis_run_id, decisions=(), candidate_commercial_facts=(bundle,),
    )
    runtime = _Runtime(retrieved_candidate={"candidate-0001": frozenset({"native:p0001:b000002"})})
    issues = validate_term_applicability_result(task, result, runtime)
    assert any(issue.code == "duplicate_fact_period" and issue.correctable for issue in issues)


def test_fact_evidence_not_grounded_in_task_is_rejected():
    task = _fact_task()
    foreign_evidence = _evidence(uuid.uuid4(), "native:p0001:b000002")
    fact = _fact(evidence=(foreign_evidence,))
    bundle = CandidateCommercialFacts(candidate_id="candidate-0001", facts=(fact,))
    issues = validate_term_applicability_result(task, _result(task, (), (bundle,)), _Runtime())
    assert any(issue.code == "fact_evidence_not_grounded_in_task" and issue.correctable for issue in issues)


def test_fact_evidence_not_retrieved_this_run_is_rejected():
    """A fact citing real task evidence never confirmed by a successful retrieval is rejected."""

    task = _fact_task()
    fact = _fact(evidence=(task.evidence_by_id["native:p0001:b000002"],))
    bundle = CandidateCommercialFacts(candidate_id="candidate-0001", facts=(fact,))
    issues = validate_term_applicability_result(task, _result(task, (), (bundle,)), _Runtime())
    assert any(issue.code == "fact_evidence_not_retrieved" and issue.correctable for issue in issues)
