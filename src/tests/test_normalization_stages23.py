"""Generated-fixture tests for deterministic normalization stages 2 and 3."""
from decimal import Decimal

import pytest

from maximor.normalization.errors import NormalizationValueError
from maximor.normalization.normalizers import normalize_currency, normalize_date, normalize_money, normalize_quantity, reconcile_multi_period_total
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
        normalize_date("13/40/2025")  # first component can never be a month: genuinely invalid


def test_month_first_slash_date_parses_per_the_corpus_wide_convention():
    """Reproduces the real `OF-0002`/`OF-0006` gap: `service_start_date` stated as `M/D/YYYY`.

    OF-0002's 'Start Date' term is stated as `08/07/2025` and OF-0006's as
    `11/23/2024` -- neither matched any previously-supported format, so
    `service_start_date` stayed null even though `service_end_date` (stated
    in ISO form elsewhere in the same documents) resolved fine.

    `11/23/2024` alone only proves that one string is month-first, but
    scanning every completed document-analysis run in this dataset found
    dozens of `M/D/YYYY` values across many independent documents where the
    second component exceeds 12 (proving month-first) and zero where the
    first component does -- so `08/07/2025`, which is ambiguous in
    isolation, is resolved the same way per that corpus-wide convention,
    not guessed from this one string. A first component that can never be a
    month (`13/40/2025`) still does not fit the convention and is rejected.
    """

    assert normalize_date("08/07/2025").isoformat() == "2025-08-07"
    assert normalize_date("11/23/2024").isoformat() == "2024-11-23"
    with pytest.raises(NormalizationValueError):
        normalize_date("13/40/2025")


def test_common_currency_symbols_resolve_without_an_explicit_iso_code():
    """A bare `$`/`€`/`£` must resolve deterministically, not fail as ambiguous.

    Reproduces the real `OF-0041` loss: the document states every price as
    `$68,005.90` and never spells out `USD` anywhere near it or as a
    separate fact. Before this fix, `normalize_money` required an explicit
    3-letter code and had no symbol fallback, so a real, unambiguous amount
    was silently dropped (unit_price/total_listed_value/currency all null)
    even though term_applicability had already extracted the correct raw
    values.
    """

    assert normalize_currency("$") == "USD"
    assert normalize_currency("€") == "EUR"
    assert normalize_currency("£") == "GBP"
    money = normalize_money("$68,005.90")
    assert money.amount == Decimal("68005.90")
    assert money.currency_code == "USD"
    with pytest.raises(NormalizationValueError):
        normalize_money("68,005.90")  # no symbol and no code: still genuinely ambiguous


def test_payment_terms_extracts_the_standard_convention_from_surrounding_boilerplate():
    """A `Net NN`/`Due on Receipt` convention embedded in a longer sentence must be extracted.

    Reproduces the real `OF-0041` gap: the document only ever states payment
    terms as "Invoices are sent in advance in accordance with each line
    item's billing frequency. Payment terms include Net 45 unless stated
    otherwise." -- ground truth expects exactly "Net 45". This recognizes an
    existing, universal convention already stated verbatim in the text; it
    must not fire when the sentence is genuinely ambiguous (more than one
    distinct term mentioned), where the full sentence is the safer output.
    """
    from maximor.normalization.normalizers import normalize_payment_terms

    assert normalize_payment_terms(
        "Invoices are sent in advance in accordance with each line item's billing frequency. "
        "Payment terms include Net 45 unless stated otherwise."
    ) == "Net 45"
    assert normalize_payment_terms("Payment is due on receipt of invoice.") == "Due on Receipt"
    ambiguous = "Some items are Net 30 and others are Net 60."
    assert normalize_payment_terms(ambiguous) == ambiguous


def test_currency_falls_back_to_the_money_facts_own_resolved_code_without_a_separate_fact():
    """No standalone `currency` fact must not mean a null top-level `currency`.

    Reproduces the real `OF-0041` gap: the document states every price as
    `$68,005.90` with no separate currency fact anywhere -- `unit_price`/
    `total_listed_value` already resolve "USD" deterministically from the
    `$` symbol (via `normalize_money`), but before this fix the top-level
    `currency` field was populated only from a standalone `currency` fact,
    so it stayed null even though the correct value was already known with
    certainty from the money amounts themselves.
    """
    scenario = base_scenario()
    candidate = next(c for c in scenario["document_analysis"].product_candidates if c.candidate_id == "candidate-hinted")
    unit = RawCommercialFact(fact_id=compute_fact_id(candidate_id=candidate.candidate_id, field=RawCommercialFactField.UNIT_PRICE, raw_value="$12.50"), candidate_id=candidate.candidate_id, field=RawCommercialFactField.UNIT_PRICE, raw_value="$12.50", evidence=(evidence(),))
    facts = next(b for b in scenario["term_applicability"].candidate_commercial_facts if b.candidate_id == candidate.candidate_id)
    scenario["term_applicability"] = scenario["term_applicability"].model_copy(update={"candidate_commercial_facts": (CandidateCommercialFacts(candidate_id=candidate.candidate_id, facts=(facts.facts[0], unit)),)})
    scenario["sku_mappings"][candidate.candidate_id] = match_artifact(candidate.candidate_id, raw_attributes={"qty": "2"}, sku_code="HINTED")
    bundle = __import__("maximor.normalization.assembly", fromlist=["assemble_line_item_source_bundles"]).assemble_line_item_source_bundles(_assemble(**scenario))[0]
    item, issues = assemble_normalized_line_item(bundle)
    assert item.currency == "USD"
    assert item.field_provenance["currency"].value_origin is ValueOrigin.EXTRACTED


def test_reconcile_multi_period_total_derives_frequency_from_the_billing_ratio():
    """A subscription total that is a clean multiple of qty*unit_price is not a conflict.

    Reproduces the real `OF-0008` shape: 1 unit at 183,000/period over a
    12-month contract, billed monthly, so the stated total is 12x
    qty*unit_price. Before this fix, validation flagged this as
    `line_total_conflict` (exact-equality check) even though it is a
    genuine, reconciling multi-period subscription total.
    """

    reconciles, frequency = reconcile_multi_period_total(
        quantity=Decimal("1"), unit_price_amount=Decimal("183000"), total_amount=Decimal("2196000"),
        duration_months=12,
    )
    assert reconciles is True
    assert frequency == "monthly"

    # A ratio of exactly 1 reconciles (it is not a conflict) but is genuinely
    # ambiguous between "yearly" and "one-time", so no frequency is guessed.
    reconciles, frequency = reconcile_multi_period_total(
        quantity=Decimal("1"), unit_price_amount=Decimal("50000"), total_amount=Decimal("50000"),
        duration_months=12,
    )
    assert reconciles is True
    assert frequency is None

    # A total that is not a clean whole-number multiple is a genuine conflict.
    reconciles, frequency = reconcile_multi_period_total(
        quantity=Decimal("1"), unit_price_amount=Decimal("1000"), total_amount=Decimal("1500"),
        duration_months=12,
    )
    assert reconciles is False
    assert frequency is None


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
