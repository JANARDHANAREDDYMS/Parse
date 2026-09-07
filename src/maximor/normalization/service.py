"""Assemble provisional normalized line items from trusted source bundles.

The service accepts only Stage 1 trusted inputs/source bundles, applies pure
primitive normalizers, then the exact-match inheritance policy and validation
report. It returns an honestly incomplete draft and does not persist data or
call an agent.
"""
from decimal import Decimal, ROUND_HALF_EVEN

from maximor.document_analysis.schemas import EvidenceReference
from maximor.normalization.contracts import LineItemSourceBundle, NormalizationInput
from maximor.normalization.errors import NormalizationValueError
from maximor.normalization.normalizers import normalize_billing_enum, normalize_currency, normalize_date, normalize_money, normalize_payment_terms, normalize_period_label, normalize_quantity
from maximor.normalization.schemas import FieldProvenance, FinalOrderFormExtraction, NormalizedLineItem, PriceScheduleEntry, ProvenanceSourceType, ReviewIssue, ReviewIssueSeverity, ValueOrigin
from maximor.normalization.versions import FINAL_ORDER_FORM_SCHEMA_VERSION
from maximor.normalization.inheritance import apply_document_term_inheritance
from maximor.normalization.validation import validate_normalized_extraction
from maximor.sku_mapping.schemas import SkuMappingOutcome
from maximor.term_applicability.schemas import RawCommercialFact, RawCommercialFactField


def _evidence(fact: RawCommercialFact) -> tuple[EvidenceReference, ...]:
    """Return the fact's stable evidence references unchanged."""
    return fact.evidence


def _direct_provenance(bundle: LineItemSourceBundle, fact: RawCommercialFact) -> FieldProvenance:
    """Build candidate-fact provenance for a directly normalized value."""
    return FieldProvenance(source_type=ProvenanceSourceType.CANDIDATE_FACT, value_origin=ValueOrigin.EXTRACTED, source_candidate_id=bundle.candidate_id, source_fact_id=fact.fact_id, evidence=_evidence(fact))


def _derived_provenance(*groups: tuple[EvidenceReference, ...]) -> FieldProvenance:
    """Build derived provenance from all contributing evidence references."""
    refs: list[EvidenceReference] = []
    for group in groups:
        for ref in group:
            if ref not in refs:
                refs.append(ref)
    return FieldProvenance(source_type=ProvenanceSourceType.DERIVED, value_origin=ValueOrigin.DERIVED, evidence=tuple(refs))


def _fact_map(bundle: LineItemSourceBundle) -> dict[RawCommercialFactField, RawCommercialFact]:
    """Index raw facts deterministically, retaining the first only after schema uniqueness."""
    return {fact.field: fact for fact in (bundle.candidate_commercial_facts.facts if bundle.candidate_commercial_facts else ())}


def assemble_normalized_line_item(bundle: LineItemSourceBundle) -> tuple[NormalizedLineItem, tuple[ReviewIssue, ...]]:
    """Normalize one MATCH bundle and return a provisional item plus safe issues."""
    facts = _fact_map(bundle)
    provenance: dict[str, FieldProvenance] = {}
    issues: list[ReviewIssue] = []
    values: dict = {"schema_version": FINAL_ORDER_FORM_SCHEMA_VERSION, "source_candidate_id": bundle.candidate_id, "sku_id": bundle.sku_mapping_decision.sku_id, "sku_code": bundle.sku_mapping_decision.sku_code, "sku_name": bundle.sku_mapping_decision.sku_name}

    def direct(field: RawCommercialFactField, output: str, parser):
        fact = facts.get(field)
        if not fact:
            return None
        try:
            values[output] = parser(fact.raw_value)
            provenance[output] = _direct_provenance(bundle, fact)
        except NormalizationValueError as exc:
            issues.append(ReviewIssue(code=exc.code, message=exc.safe_message, severity=ReviewIssueSeverity.WARNING, candidate_id=bundle.candidate_id, field_name=output))
        return values.get(output)

    currency_fact = facts.get(RawCommercialFactField.CURRENCY)
    currency = None
    if currency_fact:
        try:
            currency = normalize_currency(currency_fact.raw_value); values["currency"] = currency; provenance["currency"] = _direct_provenance(bundle, currency_fact)
        except NormalizationValueError as exc:
            issues.append(ReviewIssue(code=exc.code, message=exc.safe_message, severity=ReviewIssueSeverity.WARNING, candidate_id=bundle.candidate_id, field_name="currency"))
    quantity = direct(RawCommercialFactField.QUANTITY, "quantity", normalize_quantity)
    unit = None
    if RawCommercialFactField.UNIT_PRICE in facts:
        try:
            unit = normalize_money(facts[RawCommercialFactField.UNIT_PRICE].raw_value, currency_code=currency); values["unit_price"] = unit; provenance["unit_price"] = _direct_provenance(bundle, facts[RawCommercialFactField.UNIT_PRICE])
        except NormalizationValueError as exc:
            issues.append(ReviewIssue(code=exc.code, message=exc.safe_message, severity=ReviewIssueSeverity.WARNING, candidate_id=bundle.candidate_id, field_name="unit_price"))
    total = None
    if RawCommercialFactField.TOTAL_LISTED_VALUE in facts:
        try:
            total = normalize_money(facts[RawCommercialFactField.TOTAL_LISTED_VALUE].raw_value, currency_code=currency); values["total_listed_value"] = total; provenance["total_listed_value"] = _direct_provenance(bundle, facts[RawCommercialFactField.TOTAL_LISTED_VALUE])
        except NormalizationValueError as exc:
            issues.append(ReviewIssue(code=exc.code, message=exc.safe_message, severity=ReviewIssueSeverity.WARNING, candidate_id=bundle.candidate_id, field_name="total_listed_value"))
    if quantity is not None and unit is not None and total is None:
        derived_amount = (quantity * unit.amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        values["total_listed_value"] = type(unit)(amount=derived_amount, currency_code=unit.currency_code)
        provenance["total_listed_value"] = _derived_provenance(facts[RawCommercialFactField.QUANTITY].evidence, facts[RawCommercialFactField.UNIT_PRICE].evidence)
    elif quantity is not None and unit is not None and total is not None:
        expected = (quantity * unit.amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        if expected != total.amount or unit.currency_code != total.currency_code:
            issues.append(ReviewIssue(code="conflicting_values", message="Supplied and calculated commercial values conflict.", severity=ReviewIssueSeverity.ERROR, candidate_id=bundle.candidate_id, field_name="total_listed_value"))
    elif quantity is not None and total is not None and unit is None and quantity > 0:
        derived_amount = (total.amount / quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        if derived_amount * quantity != total.amount:
            issues.append(ReviewIssue(code="non_exact_decimal_division", message="Unit-price derivation is not exact under the decimal policy.", severity=ReviewIssueSeverity.WARNING, candidate_id=bundle.candidate_id, field_name="unit_price"))
        else:
            values["unit_price"] = type(total)(amount=derived_amount, currency_code=total.currency_code)
            provenance["unit_price"] = _derived_provenance(facts[RawCommercialFactField.QUANTITY].evidence, facts[RawCommercialFactField.TOTAL_LISTED_VALUE].evidence)

    direct(RawCommercialFactField.SERVICE_START_DATE, "service_start_date", normalize_date)
    direct(RawCommercialFactField.SERVICE_END_DATE, "service_end_date", normalize_date)
    direct(RawCommercialFactField.PAYMENT_TERMS, "payment_terms", normalize_payment_terms)
    direct(RawCommercialFactField.INVOICING_FREQUENCY, "invoicing_frequency", normalize_payment_terms)
    direct(RawCommercialFactField.INVOICING_SCHEDULE_TYPE, "invoicing_schedule_type", normalize_payment_terms)
    direct(RawCommercialFactField.UNIT_PRICE_PERIOD, "unit_price_period", normalize_period_label)
    yearly = []
    for fact in sorted((f for f in facts.values() if f.field is RawCommercialFactField.YEARLY_PRICE), key=lambda f: (f.raw_period_label or "", f.fact_id)):
        try:
            amount = normalize_money(fact.raw_value, currency_code=currency)
            yearly.append(PriceScheduleEntry(period_label=normalize_period_label(fact.raw_period_label) if fact.raw_period_label else None, raw_amount=fact.raw_value, normalized_amount=amount, provenance=_direct_provenance(bundle, fact)))
        except NormalizationValueError as exc:
            issues.append(ReviewIssue(code=exc.code, message=exc.safe_message, severity=ReviewIssueSeverity.WARNING, candidate_id=bundle.candidate_id, field_name="yearly_price_schedule"))
    if yearly:
        values["yearly_price_schedule"] = tuple(yearly)
        if total is None and len({entry.normalized_amount.currency_code for entry in yearly if entry.normalized_amount}) == 1 and all(entry.normalized_amount for entry in yearly):
            schedule_total = sum((entry.normalized_amount.amount for entry in yearly), Decimal("0"))
            values["total_listed_value"] = type(yearly[0].normalized_amount)(amount=schedule_total, currency_code=yearly[0].normalized_amount.currency_code)
            provenance["total_listed_value"] = _derived_provenance(*(entry.provenance.evidence for entry in yearly))
    line = NormalizedLineItem(**values, field_provenance=provenance)
    return line, tuple(issues)


def assemble_normalized_draft(normalization_input: NormalizationInput) -> FinalOrderFormExtraction:
    """Build a provisional draft from MATCH bundles with bounded unresolved issues."""
    bundles = []
    for bundle in __import__("maximor.normalization.assembly", fromlist=["assemble_line_item_source_bundles"]).assemble_line_item_source_bundles(normalization_input):
        bundles.append(bundle)
    items, issues = [], []
    for bundle in bundles:
        item, item_issues = assemble_normalized_line_item(bundle); items.append(item); issues.extend(item_issues)
    draft = FinalOrderFormExtraction(schema_version=FINAL_ORDER_FORM_SCHEMA_VERSION, organization_id=normalization_input.organization_id, document_id=normalization_input.document_id, preprocessing_run_id=normalization_input.preprocessing_run_id, analysis_run_id=normalization_input.analysis_run_id, term_applicability_run_id=normalization_input.term_applicability_run_id, sku_mapping_run_ids=tuple(sorted(normalization_input.sku_mapping_run_ids.values(), key=str)), line_items=tuple(sorted(items, key=lambda i: i.source_candidate_id)), validation_status="review_required", review_issues=tuple(issues))
    inherited = apply_document_term_inheritance(normalization_input, draft)
    report = validate_normalized_extraction(normalization_input, inherited)
    return inherited.model_copy(update={"review_issues": tuple((*inherited.review_issues, *report.issues))})
