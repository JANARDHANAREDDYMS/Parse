"""Test the RawCommercialFact/CandidateCommercialFacts schemas and their canonical fact_id.

No database, no Claude call. `TermApplicabilityDecision` behavior is covered
by `test_term_applicability_decision.py`; this file focuses only on the
commercial-fact addition and its backward-compatible coexistence with the
pre-existing term-only result shape.
"""

import uuid

import pytest
from pydantic import ValidationError

from maximor.document_analysis.schemas import EvidenceReference, EvidenceRepresentation
from maximor.preprocessing.schemas import ExtractionSource
from maximor.term_applicability.schemas import (
    CandidateCommercialFacts,
    RawCommercialFact,
    RawCommercialFactField,
    TermApplicabilityDecision,
    TermApplicabilityResult,
    TermDisposition,
    compute_fact_id,
)
from maximor.term_applicability.versions import TERM_APPLICABILITY_RESULT_SCHEMA_VERSION


def _evidence(run_id: uuid.UUID | None = None, block_id: str = "native:p0001:b000000") -> EvidenceReference:
    return EvidenceReference(
        preprocessing_run_id=run_id or uuid.uuid4(), page_number=1, block_id=block_id,
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    )


# --- compute_fact_id --------------------------------------------------------------

def test_compute_fact_id_is_deterministic_for_identical_inputs():
    first = compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.QUANTITY, raw_value="12")
    second = compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.QUANTITY, raw_value="12")
    assert first == second


def test_compute_fact_id_distinguishes_yearly_price_periods():
    year_one = compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.YEARLY_PRICE, raw_value="$100", raw_period_label="Year 1")
    year_two = compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.YEARLY_PRICE, raw_value="$110", raw_period_label="Year 2")
    year_three = compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.YEARLY_PRICE, raw_value="$120", raw_period_label="Year 3")
    assert len({year_one, year_two, year_three}) == 3


def test_compute_fact_id_distinguishes_special_note_content():
    """Two special_note facts on the same candidate must not collide on ID."""

    first = compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.SPECIAL_NOTE, raw_value="Auto-renews annually")
    second = compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.SPECIAL_NOTE, raw_value="Price locked for term")
    assert first != second


def test_compute_fact_id_ignores_raw_value_for_non_note_fields():
    """A field's raw text does not distinguish a fact_id -- only period context does."""

    first = compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.QUANTITY, raw_value="10")
    second = compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.QUANTITY, raw_value="20")
    assert first == second


# --- RawCommercialFact -------------------------------------------------------------

def test_raw_commercial_fact_requires_the_canonical_fact_id():
    fact_id = compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.UNIT_PRICE, raw_value="$5.00")
    fact = RawCommercialFact(
        fact_id=fact_id, candidate_id="candidate-0001", field=RawCommercialFactField.UNIT_PRICE,
        raw_value="$5.00", evidence=(_evidence(),),
    )
    assert fact.fact_id == fact_id


def test_raw_commercial_fact_rejects_a_non_canonical_fact_id():
    """A fact_id that Claude might invent, rather than compute, is rejected."""

    with pytest.raises(ValidationError):
        RawCommercialFact(
            fact_id="fact:not-the-real-id", candidate_id="candidate-0001",
            field=RawCommercialFactField.UNIT_PRICE, raw_value="$5.00", evidence=(_evidence(),),
        )


def test_raw_commercial_fact_requires_at_least_one_evidence_reference():
    fact_id = compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.QUANTITY, raw_value="10")
    with pytest.raises(ValidationError):
        RawCommercialFact(
            fact_id=fact_id, candidate_id="candidate-0001", field=RawCommercialFactField.QUANTITY,
            raw_value="10", evidence=(),
        )


def test_raw_commercial_fact_rejects_unsupported_field_name():
    with pytest.raises(ValidationError):
        RawCommercialFact(
            fact_id="fact:candidate-0001:sku_mapping", candidate_id="candidate-0001",
            field="sku_mapping", raw_value="SKU-1", evidence=(_evidence(),),
        )


def test_raw_commercial_fact_is_frozen_and_forbids_extra():
    fact_id = compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.QUANTITY, raw_value="10")
    fact = RawCommercialFact(
        fact_id=fact_id, candidate_id="candidate-0001", field=RawCommercialFactField.QUANTITY,
        raw_value="10", evidence=(_evidence(),),
    )
    with pytest.raises(ValidationError):
        fact.raw_value = "20"
    with pytest.raises(ValidationError):
        RawCommercialFact(
            fact_id=fact_id, candidate_id="candidate-0001", field=RawCommercialFactField.QUANTITY,
            raw_value="10", evidence=(_evidence(),), unexpected_field="x",
        )


# --- CandidateCommercialFacts -------------------------------------------------------

def _fact(candidate_id: str, field: RawCommercialFactField, raw_value: str, raw_period_label: str | None = None) -> RawCommercialFact:
    return RawCommercialFact(
        fact_id=compute_fact_id(candidate_id=candidate_id, field=field, raw_value=raw_value, raw_period_label=raw_period_label),
        candidate_id=candidate_id, field=field, raw_value=raw_value, raw_period_label=raw_period_label,
        evidence=(_evidence(),),
    )


def test_candidate_commercial_facts_accepts_multiple_yearly_price_periods():
    bundle = CandidateCommercialFacts(
        candidate_id="candidate-0001",
        facts=(
            _fact("candidate-0001", RawCommercialFactField.YEARLY_PRICE, "$100", "Year 1"),
            _fact("candidate-0001", RawCommercialFactField.YEARLY_PRICE, "$110", "Year 2"),
            _fact("candidate-0001", RawCommercialFactField.YEARLY_PRICE, "$120", "Year 3"),
        ),
    )
    assert len(bundle.facts) == 3


def test_candidate_commercial_facts_rejects_a_fact_belonging_to_a_different_candidate():
    with pytest.raises(ValidationError):
        CandidateCommercialFacts(
            candidate_id="candidate-0001",
            facts=(_fact("candidate-0002", RawCommercialFactField.QUANTITY, "10"),),
        )


def test_candidate_commercial_facts_rejects_duplicate_fact_ids():
    fact = _fact("candidate-0001", RawCommercialFactField.QUANTITY, "10")
    with pytest.raises(ValidationError):
        CandidateCommercialFacts(candidate_id="candidate-0001", facts=(fact, fact))


# --- TermApplicabilityResult: decisions and facts are structurally disjoint --------

def test_result_carries_both_decisions_and_candidate_commercial_facts_independently():
    """A document_metadata decision and a candidate fact bundle coexist with no shared ID space."""

    decision = TermApplicabilityDecision(schema_version="1.0.0", term_id="term-0001", disposition=TermDisposition.DOCUMENT_METADATA)
    bundle = CandidateCommercialFacts(candidate_id="candidate-0001", facts=(_fact("candidate-0001", RawCommercialFactField.QUANTITY, "10"),))
    result = TermApplicabilityResult(
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
        decisions=(decision,), candidate_commercial_facts=(bundle,),
    )
    assert result.decisions[0].term_id == "term-0001"
    assert result.candidate_commercial_facts[0].candidate_id == "candidate-0001"


def test_result_rejects_duplicate_or_unordered_candidate_commercial_facts():
    with pytest.raises(ValidationError):
        TermApplicabilityResult(
            schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
            preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
            candidate_commercial_facts=(
                CandidateCommercialFacts(candidate_id="candidate-0002", facts=()),
                CandidateCommercialFacts(candidate_id="candidate-0001", facts=()),
            ),
        )


# --- Backward compatibility: pre-existing term-only results still load ------------

def test_old_term_only_result_json_still_loads_with_empty_facts():
    """A result persisted before this field existed remains loadable, never a migration."""

    old_shaped = {
        "schema_version": "1.0.0",
        "organization_id": str(uuid.uuid4()),
        "document_id": str(uuid.uuid4()),
        "preprocessing_run_id": str(uuid.uuid4()),
        "analysis_run_id": str(uuid.uuid4()),
        "decisions": [
            {"schema_version": "1.0.0", "term_id": "term-0001", "disposition": "document_metadata"},
        ],
    }
    result = TermApplicabilityResult.model_validate(old_shaped)
    assert result.candidate_commercial_facts == ()
    assert result.decisions[0].term_id == "term-0001"


def test_old_canonical_result_with_explicit_extracted_fields_still_loads():
    """A result persisted before the live finalizer stopped accepting `extracted_fields`
    from Claude still deserializes -- the canonical schema field itself never changed,
    only which callers are trusted to populate it live."""

    fact_id = compute_fact_id(candidate_id="candidate-0001", field=RawCommercialFactField.QUANTITY, raw_value="10")
    old_shaped = {
        "schema_version": "1.2.0",
        "organization_id": str(uuid.uuid4()),
        "document_id": str(uuid.uuid4()),
        "preprocessing_run_id": str(uuid.uuid4()),
        "analysis_run_id": str(uuid.uuid4()),
        "decisions": [],
        "candidate_commercial_facts": [
            {"candidate_id": "candidate-0001", "facts": [
                {"fact_id": fact_id, "candidate_id": "candidate-0001", "field": "quantity", "raw_value": "10", "evidence": [_evidence().model_dump(mode="json")]},
            ]},
        ],
        "candidate_commercial_fact_coverage": [
            {"candidate_id": "candidate-0001", "expected_fields": ["quantity"], "extracted_fields": ["quantity"], "unresolved_fields": [], "evidence": [_evidence().model_dump(mode="json")]},
        ],
    }
    result = TermApplicabilityResult.model_validate(old_shaped)
    assert result.candidate_commercial_fact_coverage[0].extracted_fields == (RawCommercialFactField.QUANTITY,)
