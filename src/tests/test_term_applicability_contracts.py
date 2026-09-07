"""Test the term-applicability task/result contracts without database or agent work."""

import uuid

import pytest
from pydantic import ValidationError

from maximor.document_analysis.schemas import (
    ApplicabilityScope,
    CommercialStatus,
    CommercialStatusAssessment,
    DocumentAnalysisResult,
    EvidenceReference,
    EvidenceRepresentation,
    GlobalTerm,
    ProductCandidate,
)
from maximor.preprocessing.schemas import ExtractionSource
from maximor.term_applicability.agent import TermApplicabilityAgent, UnconfiguredTermApplicabilityAgent
from maximor.term_applicability.contracts import (
    CandidateContext,
    TermApplicabilityTask,
    TermContext,
    build_selected_term_applicability_task,
    build_term_applicability_result,
    build_term_applicability_task,
)
from maximor.term_applicability.errors import (
    TermApplicabilityNotConfiguredError,
    TermApplicabilityResultConstructionError,
    TermApplicabilityTaskConstructionError,
)
from maximor.term_applicability.schemas import (
    CandidateCommercialFacts,
    RawCommercialFact,
    RawCommercialFactField,
    TermApplicabilityDecision,
    TermApplicabilityResult,
    TermDisposition,
    compute_fact_id,
)
from maximor.term_applicability.versions import TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, TERM_APPLICABILITY_TASK_SCHEMA_VERSION
from maximor.term_triage.schemas import TermTriageDecision, TermTriageDisposition, TermTriageResult


def _evidence(preprocessing_run_id: uuid.UUID, block_id: str = "native:p0001:b000000") -> EvidenceReference:
    return EvidenceReference(
        preprocessing_run_id=preprocessing_run_id, page_number=1, block_id=block_id,
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    )


def _result(preprocessing_run_id: uuid.UUID, *, terms: tuple[GlobalTerm, ...] = ()) -> DocumentAnalysisResult:
    candidate = ProductCandidate(
        candidate_id="candidate-0001", raw_name="Talent Acquisition",
        raw_attributes={"sku_description": "Talent Acquisition module"},
        evidence=(_evidence(preprocessing_run_id),),
    )
    status = CommercialStatusAssessment(
        assessment_id="status-0001", status=CommercialStatus.PURCHASED, candidate_id="candidate-0001",
        evidence=(_evidence(preprocessing_run_id),),
    )
    return DocumentAnalysisResult(
        schema_version="analysis-v1", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=preprocessing_run_id, preprocessing_schema_version="prep-v1",
        prompt_version="prompt-v1", agent_version="agent-v1",
        product_candidates=(candidate,), commercial_statuses=(status,), global_terms=terms,
    )


# --- build_term_applicability_task ---------------------------------------------

def test_build_term_applicability_task_from_valid_result():
    """A candidate and term with linked status/evidence build a compact batch task."""

    run_id = uuid.uuid4()
    term = GlobalTerm(term_id="term-0001", raw_name="Payment terms", raw_value="Net 45", evidence=(_evidence(run_id),))
    result = _result(run_id, terms=(term,))
    task = build_term_applicability_task(result=result, analysis_run_id=uuid.uuid4(), schema_version=TERM_APPLICABILITY_TASK_SCHEMA_VERSION)
    assert task.candidate_ids == ("candidate-0001",)
    assert task.term_ids == ("term-0001",)
    assert task.candidates[0].commercial_status == CommercialStatus.PURCHASED
    assert task.candidates[0].evidence_ids == ("native:p0001:b000000",)
    assert task.terms[0].raw_value == "Net 45"
    assert task.document_analysis_schema_version == "analysis-v1"


def test_build_term_applicability_task_rejects_candidate_without_status():
    """A candidate lacking a linked commercial-status assessment is a construction error."""

    run_id = uuid.uuid4()
    candidate = ProductCandidate(candidate_id="candidate-0001", raw_name="X", evidence=(_evidence(run_id),))
    result = DocumentAnalysisResult(
        schema_version="analysis-v1", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=run_id, preprocessing_schema_version="prep-v1",
        prompt_version="p", agent_version="a", product_candidates=(candidate,),
    )
    with pytest.raises(TermApplicabilityTaskConstructionError) as captured:
        build_term_applicability_task(result=result, analysis_run_id=uuid.uuid4(), schema_version="1.0.0")
    assert captured.value.code == "term_applicability_status_missing"


def test_task_evidence_ids_are_bare_strings_not_evidence_references():
    """The task carries compact evidence IDs, never full EvidenceReference objects."""

    run_id = uuid.uuid4()
    term = GlobalTerm(term_id="term-0001", raw_name="X", evidence=(_evidence(run_id),))
    result = _result(run_id, terms=(term,))
    task = build_term_applicability_task(result=result, analysis_run_id=uuid.uuid4(), schema_version="1.0.0")
    assert task.terms[0].evidence_ids == ("native:p0001:b000000",)
    assert all(isinstance(item, str) for item in task.terms[0].evidence_ids)
    assert all(isinstance(item, str) for item in task.candidates[0].evidence_ids)


# --- TermApplicabilityTask self-consistency -------------------------------------

def test_task_rejects_duplicate_or_unordered_candidates_and_terms():
    """Task construction requires unique, ascending candidate_id and term_id."""

    with pytest.raises(ValidationError):
        TermApplicabilityTask(
            schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
            preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
            document_analysis_schema_version="a", document_analysis_agent_version="a",
            candidates=(
                CandidateContext(candidate_id="candidate-0002", raw_name="A", commercial_status=CommercialStatus.PURCHASED),
                CandidateContext(candidate_id="candidate-0001", raw_name="B", commercial_status=CommercialStatus.PURCHASED),
            ),
        )
    with pytest.raises(ValidationError):
        TermApplicabilityTask(
            schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
            preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
            document_analysis_schema_version="a", document_analysis_agent_version="a",
            terms=(
                TermContext(term_id="term-0001", raw_name="A"),
                TermContext(term_id="term-0001", raw_name="B"),
            ),
        )


def test_task_and_context_types_are_frozen_and_forbid_extra():
    """TermApplicabilityTask and its contexts reject unknown fields and mutation."""

    task = TermApplicabilityTask(
        schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="a", document_analysis_agent_version="a",
    )
    with pytest.raises(ValidationError):
        task.schema_version = "2.0.0"
    with pytest.raises(ValidationError):
        TermApplicabilityTask(
            schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
            preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
            document_analysis_schema_version="a", document_analysis_agent_version="a",
            unexpected_field="x",
        )


# --- build_term_applicability_result and unknown-reference rejection ------------

def _task(*, candidate_ids=("candidate-0001",), term_ids=("term-0001",)) -> TermApplicabilityTask:
    return TermApplicabilityTask(
        schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="a", document_analysis_agent_version="a",
        candidates=tuple(CandidateContext(candidate_id=cid, raw_name="X", commercial_status=CommercialStatus.PURCHASED) for cid in candidate_ids),
        terms=tuple(TermContext(term_id=tid, raw_name="Y") for tid in term_ids),
    )


def test_build_term_applicability_result_accepts_decisions_within_task():
    """A decision naming known term/candidate IDs builds the canonical result."""

    task = _task()
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.CANDIDATE, applies_to_candidate_ids=("candidate-0001",),
        evidence=(_evidence(task.preprocessing_run_id),),
    )
    result = build_term_applicability_result(task=task, decisions=(decision,), schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION)
    assert isinstance(result, TermApplicabilityResult)
    assert result.organization_id == task.organization_id
    assert result.decisions[0].term_id == "term-0001"


def test_build_term_applicability_result_rejects_unknown_term():
    """A decision naming a term outside the task's loaded batch is rejected."""

    task = _task()
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-missing", disposition=TermDisposition.DOCUMENT_METADATA,
    )
    with pytest.raises(TermApplicabilityResultConstructionError) as captured:
        build_term_applicability_result(task=task, decisions=(decision,), schema_version="1.0.0")
    assert captured.value.code == "term_applicability_unknown_term"


def test_build_term_applicability_result_rejects_unknown_candidate():
    """A decision naming a candidate outside the task's loaded batch is rejected."""

    task = _task()
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.CANDIDATE, applies_to_candidate_ids=("candidate-missing",),
        evidence=(_evidence(task.preprocessing_run_id),),
    )
    with pytest.raises(TermApplicabilityResultConstructionError) as captured:
        build_term_applicability_result(task=task, decisions=(decision,), schema_version="1.0.0")
    assert captured.value.code == "term_applicability_unknown_candidate"


def _eligibility_task() -> TermApplicabilityTask:
    return TermApplicabilityTask(
        schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="a", document_analysis_agent_version="a",
        candidates=(
            CandidateContext(candidate_id="candidate-0001", raw_name="X", commercial_status=CommercialStatus.PURCHASED),
            CandidateContext(candidate_id="candidate-0002", raw_name="Y", commercial_status=CommercialStatus.EXCLUDED),
        ),
        terms=(TermContext(term_id="term-0001", raw_name="Z"),),
    )


def _fact(candidate_id: str) -> RawCommercialFact:
    return RawCommercialFact(
        fact_id=compute_fact_id(candidate_id=candidate_id, field=RawCommercialFactField.QUANTITY, raw_value="10"),
        candidate_id=candidate_id, field=RawCommercialFactField.QUANTITY, raw_value="10",
        evidence=(_evidence(uuid.uuid4()),),
    )


def test_build_term_applicability_result_accepts_facts_for_an_eligible_candidate():
    task = _eligibility_task()
    bundle = CandidateCommercialFacts(candidate_id="candidate-0001", facts=(_fact("candidate-0001"),))
    result = build_term_applicability_result(task=task, decisions=(), candidate_commercial_facts=(bundle,), schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION)
    assert result.candidate_commercial_facts[0].candidate_id == "candidate-0001"


def test_build_term_applicability_result_rejects_facts_for_an_unknown_candidate():
    task = _eligibility_task()
    bundle = CandidateCommercialFacts(candidate_id="candidate-missing", facts=(_fact("candidate-missing"),))
    with pytest.raises(TermApplicabilityResultConstructionError) as captured:
        build_term_applicability_result(task=task, decisions=(), candidate_commercial_facts=(bundle,), schema_version="1.0.0")
    assert captured.value.code == "term_applicability_fact_unknown_candidate"


def test_build_term_applicability_result_rejects_facts_for_an_ineligible_candidate():
    """A candidate that exists but is excluded/optional/mentioned/ambiguous cannot receive facts."""

    task = _eligibility_task()
    bundle = CandidateCommercialFacts(candidate_id="candidate-0002", facts=(_fact("candidate-0002"),))
    with pytest.raises(TermApplicabilityResultConstructionError) as captured:
        build_term_applicability_result(task=task, decisions=(), candidate_commercial_facts=(bundle,), schema_version="1.0.0")
    assert captured.value.code == "term_applicability_fact_candidate_not_eligible"


def test_result_rejects_duplicate_or_unordered_decisions():
    """TermApplicabilityResult itself enforces unique, ascending term_id ordering."""

    with pytest.raises(ValidationError):
        TermApplicabilityResult(
            schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
            preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
            decisions=(
                TermApplicabilityDecision(schema_version="1.0.0", term_id="term-0002", disposition=TermDisposition.DOCUMENT_METADATA),
                TermApplicabilityDecision(schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.DOCUMENT_METADATA),
            ),
        )


def test_result_is_frozen_and_forbids_extra():
    """TermApplicabilityResult rejects unknown fields and mutation."""

    result = TermApplicabilityResult(
        schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
    )
    with pytest.raises(ValidationError):
        result.schema_version = "2.0.0"
    with pytest.raises(ValidationError):
        TermApplicabilityResult(
            schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
            preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(), unexpected_field="x",
        )


# --- Unconfigured agent ----------------------------------------------------------

@pytest.mark.asyncio
async def test_unconfigured_agent_fails_explicitly_and_never_fabricates():
    """The placeholder agent always raises, never returns a fake result."""

    agent: TermApplicabilityAgent = UnconfiguredTermApplicabilityAgent()
    with pytest.raises(TermApplicabilityNotConfiguredError) as captured:
        await agent.resolve(_task(), object())
    assert captured.value.code == "term_applicability_not_configured"


# --- build_selected_term_applicability_task (triage selection) ------------------

def _triage_result(task: TermApplicabilityTask, dispositions: dict[str, TermTriageDisposition]) -> TermTriageResult:
    return TermTriageResult(
        schema_version="1.0.0", organization_id=task.organization_id, document_id=task.document_id,
        preprocessing_run_id=task.preprocessing_run_id, analysis_run_id=task.analysis_run_id,
        decisions=tuple(
            TermTriageDecision(term_id=term_id, disposition=disposition)
            for term_id, disposition in sorted(dispositions.items())
        ),
    )


def test_selected_task_includes_potential_line_item_and_uncertain_terms():
    """Every potential_line_item and uncertain term is selected; metadata is not."""

    task = _task(term_ids=("term-0001", "term-0002", "term-0003"))
    triage_result = _triage_result(task, {
        "term-0001": TermTriageDisposition.DOCUMENT_METADATA,
        "term-0002": TermTriageDisposition.POTENTIAL_LINE_ITEM,
        "term-0003": TermTriageDisposition.UNCERTAIN,
    })
    selected_task = build_selected_term_applicability_task(task, triage_result)
    assert selected_task.effective_selected_term_ids == ("term-0002", "term-0003")


def test_selected_task_preserves_the_full_original_term_list_for_audit():
    """document_metadata terms are excluded from selection but never removed from the task."""

    task = _task(term_ids=("term-0001", "term-0002"))
    triage_result = _triage_result(task, {
        "term-0001": TermTriageDisposition.DOCUMENT_METADATA,
        "term-0002": TermTriageDisposition.POTENTIAL_LINE_ITEM,
    })
    selected_task = build_selected_term_applicability_task(task, triage_result)
    assert selected_task.term_ids == ("term-0001", "term-0002")
    assert selected_task.effective_selected_term_ids == ("term-0002",)


def test_selected_term_ids_are_unique_stable_and_belong_to_the_task():
    task = _task(term_ids=("term-0001", "term-0002"))
    triage_result = _triage_result(task, {
        "term-0001": TermTriageDisposition.UNCERTAIN,
        "term-0002": TermTriageDisposition.UNCERTAIN,
    })
    selected_task = build_selected_term_applicability_task(task, triage_result)
    ids = selected_task.effective_selected_term_ids
    assert ids == tuple(sorted(set(ids)))
    assert set(ids).issubset(set(selected_task.term_ids))


def test_selected_task_rejects_a_triage_result_from_a_different_task_identity():
    """A tenant/run mismatch between the triage result and the task is rejected."""

    task = _task(term_ids=("term-0001",))
    mismatched = TermTriageResult(
        schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=task.document_id,
        preprocessing_run_id=task.preprocessing_run_id, analysis_run_id=task.analysis_run_id,
        decisions=(TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.UNCERTAIN),),
    )
    with pytest.raises(TermApplicabilityTaskConstructionError) as captured:
        build_selected_term_applicability_task(task, mismatched)
    assert captured.value.code == "term_applicability_triage_identity_mismatch"


def test_selected_task_rejects_a_triage_result_that_does_not_cover_the_task_exactly():
    """A triage batch missing or adding terms relative to the task is rejected."""

    task = _task(term_ids=("term-0001", "term-0002"))
    partial = _triage_result(task, {"term-0001": TermTriageDisposition.UNCERTAIN})
    with pytest.raises(TermApplicabilityTaskConstructionError) as captured:
        build_selected_term_applicability_task(task, partial)
    assert captured.value.code == "term_applicability_triage_coverage_mismatch"


def test_applicability_rejects_a_decision_for_a_term_not_selected():
    """A decision for a real task term that triage did not select is rejected."""

    task = _task(term_ids=("term-0001", "term-0002"))
    triage_result = _triage_result(task, {
        "term-0001": TermTriageDisposition.DOCUMENT_METADATA,
        "term-0002": TermTriageDisposition.POTENTIAL_LINE_ITEM,
    })
    selected_task = build_selected_term_applicability_task(task, triage_result)
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.DOCUMENT_METADATA,
    )
    with pytest.raises(TermApplicabilityResultConstructionError) as captured:
        build_term_applicability_result(task=selected_task, decisions=(decision,), schema_version="1.0.0")
    assert captured.value.code == "term_applicability_term_not_selected"


def test_untriaged_task_still_selects_every_term_by_default():
    """Backward compatibility: a task never passed through triage treats every term as selected."""

    task = _task(term_ids=("term-0001", "term-0002"))
    assert task.selected_term_ids is None
    assert task.effective_selected_term_ids == task.term_ids == ("term-0001", "term-0002")
