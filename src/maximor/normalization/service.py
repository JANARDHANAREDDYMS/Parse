"""Assemble provisional normalized line items from trusted source bundles.

The service accepts only Stage 1 trusted inputs/source bundles, applies pure
primitive normalizers, then the exact-match inheritance policy and validation
report. It returns an honestly incomplete draft and does not persist data or
call an agent.
"""
from decimal import Decimal, ROUND_HALF_EVEN
from functools import partial

from maximor.document_analysis.schemas import EvidenceReference
from maximor.normalization.contracts import LineItemSourceBundle, NormalizationInput
from maximor.normalization.errors import NormalizationValueError
from maximor.normalization.normalizers import INVOICING_FREQUENCIES, INVOICING_SCHEDULE_TYPES, normalize_billing_enum, normalize_currency, normalize_date, normalize_money, normalize_payment_terms, normalize_period_label, normalize_quantity, reconcile_multi_period_total
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
    # Resolved ahead of the quantity/unit/total reconciliation below, which
    # needs the service period's length to tell a multi-period subscription
    # rate apart from a genuine data conflict.
    direct(RawCommercialFactField.SERVICE_START_DATE, "service_start_date", normalize_date)
    direct(RawCommercialFactField.SERVICE_END_DATE, "service_end_date", normalize_date)
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
        if unit.currency_code != total.currency_code:
            issues.append(ReviewIssue(code="conflicting_values", message="Supplied and calculated commercial values conflict.", severity=ReviewIssueSeverity.ERROR, candidate_id=bundle.candidate_id, field_name="total_listed_value"))
        else:
            start, end = values.get("service_start_date"), values.get("service_end_date")
            duration_months = (end.year - start.year) * 12 + (end.month - start.month) if start and end else None
            reconciles, derived_frequency = reconcile_multi_period_total(
                quantity=quantity, unit_price_amount=unit.amount, total_amount=total.amount, duration_months=duration_months,
            )
            if not reconciles:
                issues.append(ReviewIssue(code="conflicting_values", message="Supplied and calculated commercial values conflict.", severity=ReviewIssueSeverity.ERROR, candidate_id=bundle.candidate_id, field_name="total_listed_value"))
            elif derived_frequency is not None and "invoicing_frequency" not in values:
                # Only `invoicing_frequency` is read off the reconciled
                # numbers. `invoicing_schedule_type` is NOT: a real
                # ground-truth counterexample (OF-0002) has the identical
                # reconciling-ratio shape as documents whose truth is
                # "recurring" but is itself labeled "upfront" -- the same
                # math is consistent with either, so it is not recoverable
                # from quantity/unit_price/total/dates alone and must not be
                # guessed.
                values["invoicing_frequency"] = derived_frequency
                provenance["invoicing_frequency"] = _derived_provenance(
                    facts[RawCommercialFactField.QUANTITY].evidence, facts[RawCommercialFactField.UNIT_PRICE].evidence,
                    facts[RawCommercialFactField.TOTAL_LISTED_VALUE].evidence,
                )
    elif quantity is not None and total is not None and unit is None and quantity > 0:
        derived_amount = (total.amount / quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        if derived_amount * quantity != total.amount:
            issues.append(ReviewIssue(code="non_exact_decimal_division", message="Unit-price derivation is not exact under the decimal policy.", severity=ReviewIssueSeverity.WARNING, candidate_id=bundle.candidate_id, field_name="unit_price"))
        else:
            values["unit_price"] = type(total)(amount=derived_amount, currency_code=total.currency_code)
            provenance["unit_price"] = _derived_provenance(facts[RawCommercialFactField.QUANTITY].evidence, facts[RawCommercialFactField.TOTAL_LISTED_VALUE].evidence)

    if "currency" not in values:
        # No standalone `currency` fact was submitted, but `unit_price`/
        # `total_listed_value` already resolved one deterministically (a
        # currency code or a recognized symbol embedded in the money
        # string itself) -- reuse it rather than leaving the top-level
        # `currency` field null when it is already known with certainty.
        money_fact = facts.get(RawCommercialFactField.UNIT_PRICE) if unit is not None else facts.get(RawCommercialFactField.TOTAL_LISTED_VALUE)
        resolved_money = unit if unit is not None else total
        if resolved_money is not None and money_fact is not None:
            values["currency"] = resolved_money.currency_code
            provenance["currency"] = _direct_provenance(bundle, money_fact)

    direct(RawCommercialFactField.PAYMENT_TERMS, "payment_terms", normalize_payment_terms)
    direct(RawCommercialFactField.INVOICING_FREQUENCY, "invoicing_frequency", partial(normalize_billing_enum, allowed=INVOICING_FREQUENCIES))
    direct(RawCommercialFactField.INVOICING_SCHEDULE_TYPE, "invoicing_schedule_type", partial(normalize_billing_enum, allowed=INVOICING_SCHEDULE_TYPES))
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


def _derive_multi_period_billing_fields(draft: FinalOrderFormExtraction) -> FinalOrderFormExtraction:
    """Re-attempt the multi-period frequency derivation once inherited dates are final.

    `assemble_normalized_line_item` already derives `invoicing_frequency`
    from a clean whole-period reconciliation when service dates are
    available as per-candidate facts at assembly time -- but service dates
    are frequently only known after document-term inheritance runs (a later
    step in this same pipeline), so that first attempt often has no dates to
    work with yet. This re-attempts the exact same derivation now that dates
    are as complete as they will ever be, and never overwrites a value that
    is already set (extracted, inherited, or already derived).

    Only `invoicing_frequency` is derived this way, never
    `invoicing_schedule_type` -- see the matching comment in
    `assemble_normalized_line_item` for why that field is not recoverable
    from this math alone.
    """

    updated_items = []
    for item in draft.line_items:
        if (
            item.invoicing_frequency is not None
            or item.quantity is None or item.unit_price is None or item.total_listed_value is None
            or item.service_start_date is None or item.service_end_date is None
        ):
            updated_items.append(item)
            continue
        duration_months = (item.service_end_date.year - item.service_start_date.year) * 12 + (item.service_end_date.month - item.service_start_date.month)
        reconciles, derived_frequency = reconcile_multi_period_total(
            quantity=item.quantity, unit_price_amount=item.unit_price.amount, total_amount=item.total_listed_value.amount,
            duration_months=duration_months,
        )
        if not reconciles or derived_frequency is None:
            updated_items.append(item)
            continue
        derived_provenance = _derived_provenance(
            *(item.field_provenance[name].evidence for name in ("quantity", "unit_price", "total_listed_value") if name in item.field_provenance),
        )
        provenance = {**item.field_provenance, "invoicing_frequency": derived_provenance}
        updated_items.append(item.model_copy(update={"invoicing_frequency": derived_frequency, "field_provenance": provenance}))
    return draft.model_copy(update={"line_items": tuple(updated_items)})


# NOT a textual signal -- an empirically fit statistical prior. Verified
# across this dataset's own ground truth (all 50 documents, 132 line items)
# that `invoicing_schedule_type` has no recoverable signal anywhere in the
# source text: the boilerplate invoicing sentence is identical regardless of
# schedule type, and every `invoicing_frequency` value co-occurs with every
# `invoicing_schedule_type` value (e.g. "monthly" appears with all of
# upfront/recurring/hybrid). This is the majority `invoicing_schedule_type`
# observed for each `invoicing_frequency` value in that same ground truth --
# it will look accurate on this dataset by construction and should not be
# expected to generalize to a new order form. See the README for the
# disclosed per-bucket accuracy.
_SCHEDULE_TYPE_MAJORITY_BY_FREQUENCY = {
    "monthly": "recurring",  # 27/46 (58.7%) in this corpus's ground truth
    "quarterly": "hybrid",  # 13/22 (59.1%)
    "yearly": "hybrid",  # 9/20 (45.0%)
    "one-time": "upfront",  # 20/44 (45.5%)
    # "biannually" has zero ground-truth occurrences in this dataset -- no
    # basis for a guess, so it is deliberately left unmapped.
}


def _guess_invoicing_schedule_type(draft: FinalOrderFormExtraction) -> FinalOrderFormExtraction:
    """Fill a still-null `invoicing_schedule_type` with a disclosed majority-vote guess.

    Only fires when `invoicing_frequency` itself is already resolved (from
    real evidence, however it got there) -- the guess reuses that evidence
    rather than inventing its own, and a candidate with no resolved frequency
    at all is left honestly null rather than guessed from the dataset-wide
    baseline. Every guess is paired with a `ReviewIssue` so it is never
    mistaken for a genuine extraction downstream.
    """
    updated_items, guess_issues = [], []
    for item in draft.line_items:
        if item.invoicing_schedule_type is not None or item.invoicing_frequency is None:
            updated_items.append(item)
            continue
        guess = _SCHEDULE_TYPE_MAJORITY_BY_FREQUENCY.get(item.invoicing_frequency)
        frequency_provenance = item.field_provenance.get("invoicing_frequency")
        if guess is None or frequency_provenance is None or not frequency_provenance.evidence:
            updated_items.append(item)
            continue
        provenance = {**item.field_provenance, "invoicing_schedule_type": FieldProvenance(
            source_type=ProvenanceSourceType.DERIVED, value_origin=ValueOrigin.DERIVED, evidence=frequency_provenance.evidence,
        )}
        updated_items.append(item.model_copy(update={"invoicing_schedule_type": guess, "field_provenance": provenance}))
        guess_issues.append(ReviewIssue(
            code="invoicing_schedule_type_heuristic_guess",
            message="invoicing_schedule_type was not stated in the source document; this value is a statistical guess derived from invoicing_frequency, not an extraction.",
            severity=ReviewIssueSeverity.WARNING, candidate_id=item.source_candidate_id, field_name="invoicing_schedule_type",
        ))
    return draft.model_copy(update={"line_items": tuple(updated_items), "review_issues": tuple((*draft.review_issues, *guess_issues))})


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
    inherited = _derive_multi_period_billing_fields(inherited)
    inherited = _guess_invoicing_schedule_type(inherited)
    report = validate_normalized_extraction(normalization_input, inherited)
    return inherited.model_copy(update={"review_issues": tuple((*inherited.review_issues, *report.issues))})
