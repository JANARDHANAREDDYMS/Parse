"""Deterministic validation and reconciliation for provisional normalized output."""
from decimal import Decimal
from dataclasses import dataclass
from maximor.document_analysis.schemas import CommercialStatus
from maximor.normalization.contracts import NormalizationInput
from maximor.normalization.normalizers import reconcile_multi_period_total
from maximor.normalization.schemas import FinalOrderFormExtraction, ReviewIssue, ReviewIssueSeverity
from maximor.term_applicability.contracts import FACT_ELIGIBLE_COMMERCIAL_STATUSES

@dataclass(frozen=True)
class ValidationReport:
    """Bounded deterministic observations; no repair or semantic inference occurs."""
    issues: tuple[ReviewIssue, ...]
    @property
    def requires_review(self) -> bool:
        return bool(self.issues)

def validate_normalized_extraction(normalization_input: NormalizationInput, extraction: FinalOrderFormExtraction, *, tolerance: Decimal = Decimal("0.01")) -> ValidationReport:
    """Check lineage, line-item eligibility, periods, currencies, schedules, and totals."""
    issues = []
    candidates = {c.candidate_id: c for c in normalization_input.document_analysis.product_candidates}
    statuses = {s.candidate_id: s.status for s in normalization_input.document_analysis.commercial_statuses}
    for item in extraction.line_items:
        candidate = candidates.get(item.source_candidate_id); status = statuses.get(item.source_candidate_id)
        if candidate is None or status not in FACT_ELIGIBLE_COMMERCIAL_STATUSES:
            issues.append(ReviewIssue(code="ineligible_line_item", message="A line item is not backed by an eligible candidate.", severity=ReviewIssueSeverity.ERROR, candidate_id=item.source_candidate_id))
        if item.quantity is not None and item.quantity <= 0:
            issues.append(ReviewIssue(code="non_positive_quantity", message="Quantity must be positive.", severity=ReviewIssueSeverity.ERROR, candidate_id=item.source_candidate_id, field_name="quantity"))
        if item.service_start_date and item.service_end_date and item.service_start_date > item.service_end_date:
            issues.append(ReviewIssue(code="service_date_order_invalid", message="Service start date is after service end date.", severity=ReviewIssueSeverity.ERROR, candidate_id=item.source_candidate_id))
        currencies = {v.currency_code for v in (item.unit_price, item.total_listed_value) if v is not None}
        currencies.update(e.normalized_amount.currency_code for e in item.yearly_price_schedule if e.normalized_amount)
        if len(currencies) > 1:
            issues.append(ReviewIssue(code="incompatible_currencies", message="Line-item monetary values use incompatible currencies.", severity=ReviewIssueSeverity.ERROR, candidate_id=item.source_candidate_id))
        if item.quantity is not None and item.unit_price and item.total_listed_value and item.unit_price.currency_code == item.total_listed_value.currency_code:
            duration_months = None
            if item.service_start_date and item.service_end_date:
                duration_months = (item.service_end_date.year - item.service_start_date.year) * 12 + (item.service_end_date.month - item.service_start_date.month)
            reconciles, _ = reconcile_multi_period_total(
                quantity=item.quantity, unit_price_amount=item.unit_price.amount, total_amount=item.total_listed_value.amount,
                duration_months=duration_months, tolerance=tolerance,
            )
            if not reconciles:
                issues.append(ReviewIssue(code="line_total_conflict", message="Line total does not reconcile with quantity and unit price.", severity=ReviewIssueSeverity.ERROR, candidate_id=item.source_candidate_id, field_name="total_listed_value"))
        if item.yearly_price_schedule and item.total_listed_value and all(e.normalized_amount for e in item.yearly_price_schedule):
            total = sum((e.normalized_amount.amount for e in item.yearly_price_schedule), Decimal("0"))
            if abs(total - item.total_listed_value.amount) > tolerance:
                issues.append(ReviewIssue(code="schedule_total_conflict", message="Price schedule does not reconcile with total.", severity=ReviewIssueSeverity.ERROR, candidate_id=item.source_candidate_id))
    order_currencies = {i.currency for i in extraction.line_items if i.currency}
    if len(order_currencies) > 1:
        issues.append(ReviewIssue(code="multiple_order_currencies", message="Line items use multiple currencies.", severity=ReviewIssueSeverity.WARNING))
    issues.sort(key=lambda issue: (issue.candidate_id or "", issue.field_name or "", issue.code))
    return ValidationReport(issues=tuple(issues))
