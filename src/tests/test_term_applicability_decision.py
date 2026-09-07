"""Test TermApplicabilityDecision's disposition/scope/candidate-ID/evidence rules."""

import uuid

import pytest
from pydantic import ValidationError

from maximor.document_analysis.schemas import ApplicabilityScope, EvidenceReference, EvidenceRepresentation
from maximor.preprocessing.schemas import ExtractionSource
from maximor.term_applicability.schemas import TermApplicabilityDecision, TermDisposition


def _evidence(run_id: uuid.UUID | None = None) -> EvidenceReference:
    return EvidenceReference(
        preprocessing_run_id=run_id or uuid.uuid4(), page_number=1, block_id="native:p0001:b000000",
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    )


# --- valid combinations ----------------------------------------------------------

def test_line_item_document_scope_requires_no_candidate_ids():
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(_evidence(),),
    )
    assert decision.applies_to_candidate_ids == ()


def test_line_item_unknown_scope_requires_no_candidate_ids():
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.UNKNOWN, evidence=(_evidence(),),
    )
    assert decision.applies_to_candidate_ids == ()


def test_line_item_candidate_scope_requires_ordered_unique_candidate_ids():
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.CANDIDATE,
        applies_to_candidate_ids=("candidate-0001", "candidate-0002"), evidence=(_evidence(),),
    )
    assert decision.applies_to_candidate_ids == ("candidate-0001", "candidate-0002")


def test_document_metadata_requires_no_scope_and_no_candidate_ids_and_no_evidence():
    decision = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.DOCUMENT_METADATA,
    )
    assert decision.applicability_scope is None
    assert decision.applies_to_candidate_ids == ()


# --- invalid combinations ---------------------------------------------------------

def test_line_item_requires_a_scope():
    with pytest.raises(ValidationError):
        TermApplicabilityDecision(schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM, evidence=(_evidence(),))


def test_line_item_candidate_scope_requires_at_least_one_candidate_id():
    with pytest.raises(ValidationError):
        TermApplicabilityDecision(
            schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
            applicability_scope=ApplicabilityScope.CANDIDATE, evidence=(_evidence(),),
        )


def test_line_item_document_scope_rejects_candidate_ids():
    with pytest.raises(ValidationError):
        TermApplicabilityDecision(
            schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
            applicability_scope=ApplicabilityScope.DOCUMENT, applies_to_candidate_ids=("candidate-0001",),
            evidence=(_evidence(),),
        )


def test_line_item_unknown_scope_rejects_candidate_ids():
    with pytest.raises(ValidationError):
        TermApplicabilityDecision(
            schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
            applicability_scope=ApplicabilityScope.UNKNOWN, applies_to_candidate_ids=("candidate-0001",),
            evidence=(_evidence(),),
        )


def test_candidate_ids_must_be_unique():
    with pytest.raises(ValidationError):
        TermApplicabilityDecision(
            schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
            applicability_scope=ApplicabilityScope.CANDIDATE,
            applies_to_candidate_ids=("candidate-0001", "candidate-0001"), evidence=(_evidence(),),
        )


def test_candidate_ids_must_be_lexically_ordered():
    with pytest.raises(ValidationError):
        TermApplicabilityDecision(
            schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
            applicability_scope=ApplicabilityScope.CANDIDATE,
            applies_to_candidate_ids=("candidate-0002", "candidate-0001"), evidence=(_evidence(),),
        )


def test_line_item_requires_at_least_one_evidence_reference():
    with pytest.raises(ValidationError):
        TermApplicabilityDecision(
            schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.LINE_ITEM,
            applicability_scope=ApplicabilityScope.UNKNOWN,
        )


def test_document_metadata_rejects_a_scope():
    with pytest.raises(ValidationError):
        TermApplicabilityDecision(
            schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.DOCUMENT_METADATA,
            applicability_scope=ApplicabilityScope.UNKNOWN,
        )


def test_document_metadata_rejects_candidate_ids():
    with pytest.raises(ValidationError):
        TermApplicabilityDecision(
            schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.DOCUMENT_METADATA,
            applies_to_candidate_ids=("candidate-0001",),
        )


def test_decision_is_frozen_and_forbids_extra_fields():
    decision = TermApplicabilityDecision(schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.DOCUMENT_METADATA)
    with pytest.raises(ValidationError):
        decision.term_id = "term-9999"
    with pytest.raises(ValidationError):
        TermApplicabilityDecision(
            schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.DOCUMENT_METADATA,
            unexpected_field="x",
        )


def test_rationale_is_bounded():
    with pytest.raises(ValidationError):
        TermApplicabilityDecision(
            schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.DOCUMENT_METADATA,
            rationale="x" * 2_001,
        )
