"""Test normalization contract-level invariants directly (not through assembly).

No database, no PDF, no paid Claude call. Reuses the in-memory fixture
builders from `test_normalization_assembly` -- the same pattern this
codebase already uses for cross-file fixture reuse (e.g.
`from test_sku_mapping_worker_integration import _seed`).
"""

import uuid

import pytest
from pydantic import ValidationError

from maximor.document_analysis.schemas import ApplicabilityScope, CommercialStatus, EvidenceReference, EvidenceRepresentation, ExtractionSource
from maximor.normalization.contracts import LineItemSourceBundle, NormalizationInput
from maximor.normalization.schemas import (
    FieldProvenance,
    FinalOrderFormExtraction,
    MoneyValue,
    NormalizedLineItem,
    ProvenanceSourceType,
    ValueOrigin,
)
from maximor.sku_mapping.schemas import SkuMappingDecision, SkuMappingOutcome
from maximor.term_applicability.schemas import RawCommercialFactField, TermApplicabilityDecision, TermDisposition
from test_normalization_assembly import (
    ANALYSIS_RUN_ID,
    DOCUMENT_ID,
    ORGANIZATION_ID,
    PREPROCESSING_RUN_ID,
    TERM_APPLICABILITY_RUN_ID,
    base_scenario,
    evidence,
    match_artifact,
)


def _replace(instance, **overrides):
    """Rebuild a frozen model through its constructor so validators actually re-run.

    `model_copy(update=...)` deliberately skips validation in Pydantic v2 --
    fine for trusted internal copies, useless for testing that a validator
    rejects a bad value.
    """

    cls = type(instance)
    fields = {name: getattr(instance, name) for name in cls.model_fields}
    fields.update(overrides)
    return cls(**fields)


def _assembled_input() -> NormalizationInput:
    from maximor.normalization.assembly import assemble_normalization_input

    scenario = base_scenario()
    return assemble_normalization_input(
        organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID, analysis_run_id=ANALYSIS_RUN_ID,
        term_applicability_run_id=TERM_APPLICABILITY_RUN_ID,
        document_analysis=scenario["document_analysis"], term_triage=scenario["term_triage"],
        term_applicability=scenario["term_applicability"], sku_mappings=scenario["sku_mappings"],
        sku_mapping_run_ids=scenario["sku_mapping_run_ids"],
    )


# --- NormalizationInput --------------------------------------------------------------

def test_normalization_input_rejects_sku_mappings_not_covering_exactly_eligible_candidates():
    normalization_input = _assembled_input()
    with pytest.raises(ValidationError):
        _replace(normalization_input, sku_mappings={})


def test_normalization_input_rejects_a_sku_mapping_entry_for_the_wrong_candidate():
    normalization_input = _assembled_input()
    swapped = dict(normalization_input.sku_mappings)
    swapped["candidate-plain"] = match_artifact("candidate-hinted")
    with pytest.raises(ValidationError):
        _replace(normalization_input, sku_mappings=swapped)


def test_normalization_input_rejects_analysis_run_identity_mismatch():
    normalization_input = _assembled_input()
    with pytest.raises(ValidationError):
        _replace(normalization_input, analysis_run_id=uuid.uuid4())


# --- LineItemSourceBundle ------------------------------------------------------------

def _match_decision(candidate_id: str = "candidate-plain") -> SkuMappingDecision:
    return SkuMappingDecision(
        schema_version="decision-v1", organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID,
        preprocessing_run_id=PREPROCESSING_RUN_ID, analysis_run_id=ANALYSIS_RUN_ID, candidate_id=candidate_id,
        catalog_version_id=uuid.uuid4(), outcome=SkuMappingOutcome.MATCH,
        sku_id=uuid.uuid4(), sku_code="X", sku_name="X", evidence=(evidence(),),
    )


def _no_match_decision(candidate_id: str = "candidate-plain") -> SkuMappingDecision:
    return SkuMappingDecision(
        schema_version="decision-v1", organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID,
        preprocessing_run_id=PREPROCESSING_RUN_ID, analysis_run_id=ANALYSIS_RUN_ID, candidate_id=candidate_id,
        catalog_version_id=uuid.uuid4(), outcome=SkuMappingOutcome.NO_MATCH, evidence=(evidence(),),
    )


def _bundle_kwargs(**overrides) -> dict:
    normalization_input = _assembled_input()
    plain_candidate = next(
        c for c in normalization_input.document_analysis.product_candidates if c.candidate_id == "candidate-plain"
    )
    kwargs = dict(
        schema_version="bundle-v1", candidate_id="candidate-plain", commercial_status=CommercialStatus.PURCHASED,
        product_candidate=plain_candidate, sku_mapping_run_id=uuid.uuid4(), sku_mapping_decision=_match_decision(),
    )
    kwargs.update(overrides)
    return kwargs


def test_line_item_source_bundle_accepts_a_valid_match_bundle():
    bundle = LineItemSourceBundle(**_bundle_kwargs())
    assert bundle.candidate_id == "candidate-plain"


def test_line_item_source_bundle_rejects_a_non_match_sku_decision():
    with pytest.raises(ValidationError):
        LineItemSourceBundle(**_bundle_kwargs(sku_mapping_decision=_no_match_decision()))


def test_line_item_source_bundle_rejects_a_decision_for_a_different_candidate():
    with pytest.raises(ValidationError):
        LineItemSourceBundle(**_bundle_kwargs(sku_mapping_decision=_match_decision("candidate-other")))


def test_line_item_source_bundle_rejects_a_candidate_term_not_naming_this_candidate():
    stray_term = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-stray", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.CANDIDATE, applies_to_candidate_ids=("candidate-other",),
        evidence=(evidence(),),
    )
    with pytest.raises(ValidationError):
        LineItemSourceBundle(**_bundle_kwargs(available_candidate_terms=(stray_term,)))


def test_line_item_source_bundle_rejects_a_non_document_scope_term_in_document_terms():
    candidate_scope_term = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-candidate-scope", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.CANDIDATE, applies_to_candidate_ids=("candidate-plain",),
        evidence=(evidence(),),
    )
    with pytest.raises(ValidationError):
        LineItemSourceBundle(**_bundle_kwargs(available_document_terms=(candidate_scope_term,)))


# --- Final-output contracts: FieldProvenance ------------------------------------------

def test_field_provenance_accepts_a_valid_candidate_fact_source():
    provenance = FieldProvenance(
        source_type=ProvenanceSourceType.CANDIDATE_FACT, value_origin=ValueOrigin.EXTRACTED,
        source_candidate_id="candidate-plain", source_fact_id="fact:candidate-plain:quantity",
        evidence=(evidence(),),
    )
    assert provenance.source_type is ProvenanceSourceType.CANDIDATE_FACT


def test_field_provenance_rejects_a_candidate_fact_source_missing_fact_id():
    with pytest.raises(ValidationError):
        FieldProvenance(
            source_type=ProvenanceSourceType.CANDIDATE_FACT, value_origin=ValueOrigin.EXTRACTED,
            source_candidate_id="candidate-plain",
        )


def test_field_provenance_rejects_a_document_term_source_carrying_a_fact_id():
    with pytest.raises(ValidationError):
        FieldProvenance(
            source_type=ProvenanceSourceType.DOCUMENT_TERM, value_origin=ValueOrigin.INHERITED,
            source_term_id="term-document", source_fact_id="fact:candidate-plain:quantity",
        )


def test_field_provenance_rejects_a_derived_source_carrying_a_term_id():
    with pytest.raises(ValidationError):
        FieldProvenance(
            source_type=ProvenanceSourceType.DERIVED, value_origin=ValueOrigin.DERIVED,
            source_term_id="term-document",
        )


def test_field_provenance_accepts_a_bare_derived_source():
    provenance = FieldProvenance(source_type=ProvenanceSourceType.DERIVED, value_origin=ValueOrigin.DERIVED)
    assert provenance.source_candidate_id is None


# --- Final-output contracts: NormalizedLineItem / FinalOrderFormExtraction ------------

def test_normalized_line_item_accepts_every_optional_field_honestly_missing():
    item = NormalizedLineItem(schema_version="final-v1", source_candidate_id="candidate-plain")
    assert item.quantity is None
    assert item.unit_price is None
    assert item.field_provenance == {}


def test_normalized_line_item_never_invents_a_default_quantity():
    item = NormalizedLineItem(schema_version="final-v1", source_candidate_id="candidate-plain")
    assert item.quantity is None


def test_money_value_requires_a_three_letter_currency_code():
    with pytest.raises(ValidationError):
        MoneyValue(amount="10.00", currency_code="US")


def test_final_order_form_extraction_accepts_no_line_items_and_no_metadata():
    result = FinalOrderFormExtraction(
        schema_version="final-v1", organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID,
        preprocessing_run_id=PREPROCESSING_RUN_ID, analysis_run_id=ANALYSIS_RUN_ID,
        term_applicability_run_id=TERM_APPLICABILITY_RUN_ID,
    )
    assert result.order_metadata is None
    assert result.line_items == ()


def test_final_order_form_extraction_rejects_duplicate_sku_mapping_run_ids():
    run_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        FinalOrderFormExtraction(
            schema_version="final-v1", organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID,
            preprocessing_run_id=PREPROCESSING_RUN_ID, analysis_run_id=ANALYSIS_RUN_ID,
            term_applicability_run_id=TERM_APPLICABILITY_RUN_ID,
            sku_mapping_run_ids=(run_id, run_id),
        )


def test_final_order_form_extraction_rejects_duplicate_line_item_candidate_ids():
    item = NormalizedLineItem(schema_version="final-v1", source_candidate_id="candidate-plain")
    with pytest.raises(ValidationError):
        FinalOrderFormExtraction(
            schema_version="final-v1", organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID,
            preprocessing_run_id=PREPROCESSING_RUN_ID, analysis_run_id=ANALYSIS_RUN_ID,
            term_applicability_run_id=TERM_APPLICABILITY_RUN_ID,
            line_items=(item, item),
        )
