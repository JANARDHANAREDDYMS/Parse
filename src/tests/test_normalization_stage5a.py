"""Pure generated-fixture tests for deterministic Stage 5A readiness."""

import uuid

from maximor.normalization.finalization import assess_finalization_readiness, build_finalization_candidate
from maximor.normalization.schemas import FinalizationStatus, ValidationStatus
from maximor.normalization.service import assemble_normalized_draft
from maximor.term_applicability.schemas import CandidateCommercialFactCoverage, RawCommercialFactField
from test_normalization_assembly import _assemble, base_scenario


def test_clean_evidenced_match_is_ready_for_semantic_review_not_completed():
    normalization_input = _assemble()
    draft = assemble_normalized_draft(normalization_input)
    candidate, readiness = build_finalization_candidate(normalization_input, draft)
    assert readiness.status is FinalizationStatus.READY_FOR_SEMANTIC_REVIEW
    assert candidate.validation_status is ValidationStatus.REVIEW_REQUIRED


def test_unresolved_expected_field_is_review_required_without_a_default():
    scenario = base_scenario()
    coverage = CandidateCommercialFactCoverage(
        candidate_id="candidate-hinted",
        expected_fields=(RawCommercialFactField.QUANTITY,),
        extracted_fields=(),
        unresolved_fields=(RawCommercialFactField.QUANTITY,),
        evidence=(scenario["coverage"].evidence[0],),
    )
    scenario["term_applicability"] = scenario["term_applicability"].model_copy(update={"candidate_commercial_fact_coverage": (coverage,)})
    normalization_input = _assemble(**scenario)
    draft = assemble_normalized_draft(normalization_input)
    _, readiness = build_finalization_candidate(normalization_input, draft)
    assert readiness.status is FinalizationStatus.REVIEW_REQUIRED
    assert any(issue.code == "unresolved_commercial_field" and issue.candidate_id == "candidate-hinted" for issue in readiness.issues)
    assert draft.line_items[0].quantity is None or draft.line_items[1].quantity is None


def test_sku_identity_break_is_failed_validation():
    normalization_input = _assemble()
    draft = assemble_normalized_draft(normalization_input)
    item = draft.line_items[0].model_copy(update={"sku_code": "WRONG"})
    broken = draft.model_copy(update={"line_items": (item, *draft.line_items[1:])})
    _, readiness = build_finalization_candidate(normalization_input, broken)
    assert readiness.status is FinalizationStatus.FAILED_VALIDATION
    assert any(issue.code == "sku_identity_mismatch" and issue.hard for issue in readiness.issues)


def test_missing_field_provenance_is_a_hard_failure():
    normalization_input = _assemble()
    draft = assemble_normalized_draft(normalization_input)
    index = next(i for i, item in enumerate(draft.line_items) if item.source_candidate_id == "candidate-hinted")
    item = draft.line_items[index].model_copy(update={"field_provenance": {}})
    items = list(draft.line_items)
    items[index] = item
    readiness = assess_finalization_readiness(normalization_input, draft.model_copy(update={"line_items": tuple(items)}))
    assert readiness.status is FinalizationStatus.FAILED_VALIDATION
    assert any(issue.code == "missing_provenance" for issue in readiness.issues)


def test_lineage_mismatch_and_issue_order_are_deterministic():
    normalization_input = _assemble()
    draft = assemble_normalized_draft(normalization_input).model_copy(update={"organization_id": uuid.uuid4()})
    readiness = assess_finalization_readiness(normalization_input, draft)
    assert readiness.status is FinalizationStatus.FAILED_VALIDATION
    assert [issue.location for issue in readiness.issues] == sorted(issue.location for issue in readiness.issues)


def test_finalization_does_not_leak_sensitive_values_or_mutate_input():
    normalization_input = _assemble()
    before = normalization_input.model_dump(mode="json")
    draft = assemble_normalized_draft(normalization_input)
    candidate, _ = build_finalization_candidate(normalization_input, draft)
    serialized = str(candidate.model_dump(mode="json"))
    assert normalization_input.model_dump(mode="json") == before
    assert "/Users/" not in serialized and "SELECT " not in serialized.upper()
