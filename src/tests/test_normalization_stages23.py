"""Generated-fixture tests for deterministic normalization stages 2 and 3."""
from decimal import Decimal

import pytest

from maximor.normalization.errors import NormalizationValueError
from maximor.normalization.normalizers import normalize_currency, normalize_date, normalize_money, normalize_quantity
from maximor.normalization.service import assemble_normalized_line_item, assemble_normalized_draft
from maximor.normalization.schemas import ProvenanceSourceType, ValueOrigin
from test_normalization_assembly import _assemble, base_scenario, evidence, match_artifact
from maximor.term_applicability.schemas import CandidateCommercialFacts, RawCommercialFact, RawCommercialFactField, compute_fact_id


def test_primitive_normalizers_are_decimal_safe_and_conservative():
    assert normalize_currency("usd") == "USD"
    assert normalize_money("USD 1,250.50").amount == Decimal("1250.50")
    assert normalize_quantity("2.5 units") == Decimal("2.5")
    assert normalize_date("2025-03-04").isoformat() == "2025-03-04"
    with pytest.raises(NormalizationValueError):
        normalize_money("1,250")
    with pytest.raises(NormalizationValueError):
        normalize_quantity("0")
    with pytest.raises(NormalizationValueError):
        normalize_date("03/04/2025")


def test_line_item_assembly_preserves_provenance_and_does_not_mutate_input():
    normalization_input = _assemble()
    before = normalization_input.model_dump(mode="json")
    bundle = __import__("maximor.normalization.assembly", fromlist=["assemble_line_item_source_bundles"]).assemble_line_item_source_bundles(normalization_input)[0]
    item, issues = assemble_normalized_line_item(bundle)
    assert not issues
    assert item.quantity is None or item.quantity > 0
    assert normalization_input.model_dump(mode="json") == before
    if item.quantity is not None:
        assert item.field_provenance["quantity"].source_type is ProvenanceSourceType.CANDIDATE_FACT
        assert item.field_provenance["quantity"].value_origin is ValueOrigin.EXTRACTED


def test_missing_values_remain_absent_and_draft_is_review_required():
    normalization_input = _assemble()
    draft = assemble_normalized_draft(normalization_input)
    assert draft.validation_status.value == "review_required"
    assert all(item.quantity is None or item.quantity > 0 for item in draft.line_items)
    assert draft.order_metadata is not None and draft.order_metadata.effective_date is None


def test_quantity_and_unit_price_can_derive_total_with_decimal_provenance():
    scenario = base_scenario()
    candidate = next(c for c in scenario["document_analysis"].product_candidates if c.candidate_id == "candidate-hinted")
    unit = RawCommercialFact(fact_id=compute_fact_id(candidate_id=candidate.candidate_id, field=RawCommercialFactField.UNIT_PRICE, raw_value="12.50"), candidate_id=candidate.candidate_id, field=RawCommercialFactField.UNIT_PRICE, raw_value="12.50", evidence=(evidence(),))
    currency = RawCommercialFact(fact_id=compute_fact_id(candidate_id=candidate.candidate_id, field=RawCommercialFactField.CURRENCY, raw_value="USD"), candidate_id=candidate.candidate_id, field=RawCommercialFactField.CURRENCY, raw_value="USD", evidence=(evidence(),))
    facts = next(b for b in scenario["term_applicability"].candidate_commercial_facts if b.candidate_id == candidate.candidate_id)
    scenario["term_applicability"] = scenario["term_applicability"].model_copy(update={"candidate_commercial_facts": (CandidateCommercialFacts(candidate_id=candidate.candidate_id, facts=(facts.facts[0], unit, currency)),)})
    scenario["sku_mappings"][candidate.candidate_id] = match_artifact(candidate.candidate_id, raw_attributes={"qty": "2"}, sku_code="HINTED")
    bundle = __import__("maximor.normalization.assembly", fromlist=["assemble_line_item_source_bundles"]).assemble_line_item_source_bundles(_assemble(**scenario))[0]
    item, issues = assemble_normalized_line_item(bundle)
    assert item.total_listed_value is not None and item.total_listed_value.amount == Decimal("25.00")
    assert item.field_provenance["total_listed_value"].value_origin is ValueOrigin.DERIVED
