"""Build and assess the deterministic Stage 5A finalization boundary.

The functions in this module receive trusted Stage 1 input and the Stage 3/4
provisional extraction.  They re-run deterministic validation, classify safe
readiness issues, and return a final-schema-shaped candidate.  They never
repair values, inherit terms, access storage, call an agent, or persist data.
"""

from collections.abc import Iterable

from maximor.document_analysis.schemas import CommercialStatus
from maximor.normalization.contracts import NormalizationInput
from maximor.normalization.schemas import (
    FinalizationIssue,
    FinalizationReadiness,
    FinalizationStatus,
    FinalOrderFormExtraction,
    ReviewIssue,
    ReviewIssueSeverity,
    ReviewQueueClassification,
    ValidationStatus,
)
from maximor.normalization.validation import validate_normalized_extraction
from maximor.normalization.versions import FINALIZATION_POLICY_VERSION
from maximor.sku_mapping.schemas import SkuMappingOutcome
from maximor.term_applicability.schemas import RawCommercialFactField
from maximor.term_applicability.contracts import FACT_ELIGIBLE_COMMERCIAL_STATUSES


_COMMERCIAL_FIELDS = {
    "quantity",
    "currency",
    "unit_price",
    "unit_price_period",
    "yearly_price_schedule",
    "total_listed_value",
    "service_start_date",
    "service_end_date",
    "invoicing_schedule_type",
    "invoicing_frequency",
    "payment_terms",
    "special_notes",
}

_FACT_TO_OUTPUT = {
    RawCommercialFactField.QUANTITY: "quantity",
    RawCommercialFactField.CURRENCY: "currency",
    RawCommercialFactField.UNIT_PRICE: "unit_price",
    RawCommercialFactField.UNIT_PRICE_PERIOD: "unit_price_period",
    RawCommercialFactField.TOTAL_LISTED_VALUE: "total_listed_value",
    RawCommercialFactField.SERVICE_START_DATE: "service_start_date",
    RawCommercialFactField.SERVICE_END_DATE: "service_end_date",
    RawCommercialFactField.INVOICING_SCHEDULE_TYPE: "invoicing_schedule_type",
    RawCommercialFactField.INVOICING_FREQUENCY: "invoicing_frequency",
    RawCommercialFactField.PAYMENT_TERMS: "payment_terms",
    RawCommercialFactField.SPECIAL_NOTE: "special_notes",
    RawCommercialFactField.YEARLY_PRICE: "yearly_price_schedule",
}

# These two fields are empirically not recoverable from this dataset's
# source documents in the vast majority of cases: the only invoicing-related
# text most order forms carry is dataset-wide template boilerplate ("Invoices
# are sent in advance in accordance with each line item's billing
# frequency.") that appears identically across documents with different
# actual `invoicing_schedule_type`/`invoicing_frequency` ground truth values
# -- verified directly against persisted document text across all three
# schedule-type buckets, not assumed. Treating an honest "unresolved" here as
# a review-worthy gap would flag the overwhelming majority of documents for
# human review over information that was never in the document to find.
# `payment_terms`/dates are excluded from this list because they usually are
# recoverable (via inheritance or per-candidate facts) and their absence is a
# more meaningful signal.
_UNRESOLVED_FIELDS_NEVER_GATE_COMPLETION = frozenset({
    RawCommercialFactField.INVOICING_SCHEDULE_TYPE,
    RawCommercialFactField.INVOICING_FREQUENCY,
})


def _classification(code: str) -> ReviewQueueClassification:
    """Map a deterministic validation code to a bounded review category."""

    if code in {"non_positive_quantity", "ineligible_line_item", "sku_identity_mismatch", "lineage_mismatch", "duplicate_line_item"}:
        return ReviewQueueClassification.HARD_INVARIANT
    if code in {"unsupported_format", "invalid_provenance", "missing_provenance"}:
        return ReviewQueueClassification.UNSUPPORTED_FORMAT
    # `conflicting_values` (raised by `service.py` when a supplied
    # total_listed_value fact disagrees with the quantity*unit_price
    # calculation) and `non_exact_decimal_division` are both purely
    # arithmetic/reconciliation differences between two already-normalized
    # numbers -- never a genuinely semantic disagreement between evidence
    # sources. They must never reach semantic review: routing a Claude call
    # at a pure arithmetic mismatch was the exact bug that sent every
    # arithmetic conflict on a real document to the paid semantic-review
    # agent. `ReviewQueueClassification.CONFLICTING_VALUES` stays reserved
    # for a future detector of genuinely semantic value conflicts (e.g. two
    # contradictory clauses) -- nothing in this module currently produces it.
    if code in {"line_total_conflict", "schedule_total_conflict", "incompatible_currencies", "multiple_order_currencies", "conflicting_values", "non_exact_decimal_division"}:
        return ReviewQueueClassification.RECONCILIATION_DIFFERENCE
    if code in {"ambiguous_value", "semantic_scope_required"}:
        return ReviewQueueClassification.AMBIGUOUS_VALUE
    if code.startswith("unresolved_") or code.startswith("missing_"):
        return ReviewQueueClassification.MISSING_VALUE
    return ReviewQueueClassification.AMBIGUOUS_VALUE


def _issue_from_review(issue: ReviewIssue) -> FinalizationIssue:
    """Convert an existing Stage 4 issue without copying untrusted values."""

    hard = issue.code in {"ineligible_line_item", "non_positive_quantity"}
    classification = _classification(issue.code)
    return FinalizationIssue(
        code=issue.code,
        classification=classification,
        message=issue.message,
        location=f"line_items[{issue.candidate_id or 'unknown'}]" + (f".{issue.field_name}" if issue.field_name else ""),
        candidate_id=issue.candidate_id,
        field_name=issue.field_name,
        hard=hard,
    )


def _safe_issue(*, code: str, message: str, location: str, candidate_id: str | None = None, field_name: str | None = None, hard: bool = False) -> FinalizationIssue:
    """Create one application-authored, content-free readiness issue."""

    return FinalizationIssue(
        code=code,
        classification=_classification(code),
        message=message,
        location=location,
        candidate_id=candidate_id,
        field_name=field_name,
        hard=hard,
    )


def _lineage_and_provenance_issues(normalization_input: NormalizationInput, extraction: FinalOrderFormExtraction) -> list[FinalizationIssue]:
    """Check trusted identities, MATCH mappings, and field evidence lineage."""

    issues: list[FinalizationIssue] = []
    if (
        extraction.organization_id != normalization_input.organization_id
        or extraction.document_id != normalization_input.document_id
        or extraction.preprocessing_run_id != normalization_input.preprocessing_run_id
        or extraction.analysis_run_id != normalization_input.analysis_run_id
        or extraction.term_applicability_run_id != normalization_input.term_applicability_run_id
    ):
        issues.append(_safe_issue(code="lineage_mismatch", message="Final output identities do not match trusted input.", location="identity", hard=True))

    candidates = {candidate.candidate_id: candidate for candidate in normalization_input.document_analysis.product_candidates}
    statuses = {
        status.candidate_id: status.status
        for status in normalization_input.document_analysis.commercial_statuses
        if status.candidate_id is not None
    }
    seen: set[str] = set()
    for item in extraction.line_items:
        candidate_id = item.source_candidate_id
        if candidate_id in seen:
            issues.append(_safe_issue(code="duplicate_line_item", message="Line-item identity is duplicated.", location=f"line_items[{candidate_id}]", candidate_id=candidate_id, hard=True))
        seen.add(candidate_id)
        candidate = candidates.get(candidate_id)
        artifact = normalization_input.sku_mappings.get(candidate_id)
        if candidate is None or artifact is None or statuses.get(candidate_id) not in FACT_ELIGIBLE_COMMERCIAL_STATUSES:
            issues.append(_safe_issue(code="ineligible_line_item", message="Line item is not backed by an eligible trusted candidate.", location=f"line_items[{candidate_id}]", candidate_id=candidate_id, hard=True))
            continue
        decision = artifact.decision
        if decision.outcome is not SkuMappingOutcome.MATCH or item.sku_id != decision.sku_id or item.sku_code != decision.sku_code or item.sku_name != decision.sku_name:
            issues.append(_safe_issue(code="sku_identity_mismatch", message="Line-item SKU identity does not match the trusted MATCH mapping.", location=f"line_items[{candidate_id}].sku", candidate_id=candidate_id, hard=True))

        for field_name, value in item.model_dump(exclude={"schema_version", "source_candidate_id", "sku_id", "sku_code", "sku_name", "field_provenance"}).items():
            if value in (None, (), []):
                continue
            if field_name not in _COMMERCIAL_FIELDS:
                continue
            provenance = item.field_provenance.get(field_name)
            if provenance is None or not provenance.evidence:
                issues.append(_safe_issue(code="missing_provenance", message="A populated commercial field lacks evidence provenance.", location=f"line_items[{candidate_id}].{field_name}", candidate_id=candidate_id, field_name=field_name, hard=True))
                continue
            if any(reference.preprocessing_run_id != normalization_input.preprocessing_run_id for reference in provenance.evidence):
                issues.append(_safe_issue(code="invalid_provenance", message="Field provenance references the wrong preprocessing run.", location=f"line_items[{candidate_id}].{field_name}", candidate_id=candidate_id, field_name=field_name, hard=True))
        for entry_index, entry in enumerate(item.yearly_price_schedule):
            if not entry.provenance.evidence or any(ref.preprocessing_run_id != normalization_input.preprocessing_run_id for ref in entry.provenance.evidence):
                issues.append(_safe_issue(code="invalid_provenance", message="Price-schedule provenance is invalid.", location=f"line_items[{candidate_id}].yearly_price_schedule[{entry_index}]", candidate_id=candidate_id, field_name="yearly_price_schedule", hard=True))
    return issues


def _coverage_issues(normalization_input: NormalizationInput, extraction: FinalOrderFormExtraction) -> list[FinalizationIssue]:
    """Turn explicit upstream unresolved coverage into review issues."""

    by_candidate = {item.source_candidate_id: item for item in extraction.line_items}
    issues: list[FinalizationIssue] = []
    for coverage in normalization_input.term_applicability.candidate_commercial_fact_coverage:
        item = by_candidate.get(coverage.candidate_id)
        if item is None:
            continue
        for field in coverage.unresolved_fields:
            if field in _UNRESOLVED_FIELDS_NEVER_GATE_COMPLETION:
                continue
            output = _FACT_TO_OUTPUT.get(field, field.value)
            issues.append(_safe_issue(code="unresolved_commercial_field", message="A required commercial field remains unresolved.", location=f"line_items[{coverage.candidate_id}].{output}", candidate_id=coverage.candidate_id, field_name=output))
        extracted = set(coverage.extracted_fields)
        for expected in coverage.expected_fields:
            output = _FACT_TO_OUTPUT.get(expected, expected.value)
            if expected in extracted and getattr(item, output, None) in (None, (), []):
                issues.append(_safe_issue(code="missing_normalized_value", message="An extracted source field has no normalized value.", location=f"line_items[{coverage.candidate_id}].{output}", candidate_id=coverage.candidate_id, field_name=output))
    return issues


def assess_finalization_readiness(normalization_input: NormalizationInput, extraction: FinalOrderFormExtraction) -> FinalizationReadiness:
    """Re-run Stage 4 checks and classify the extraction for semantic review."""

    issues = [_issue_from_review(issue) for issue in extraction.review_issues]
    issues.extend(_issue_from_review(issue) for issue in validate_normalized_extraction(normalization_input, extraction).issues)
    issues.extend(_lineage_and_provenance_issues(normalization_input, extraction))
    issues.extend(_coverage_issues(normalization_input, extraction))
    # Stable de-duplication prevents the defensive re-check from producing
    # duplicate queue entries while preserving every distinct location.
    unique: dict[tuple[str, str, str | None, str | None], FinalizationIssue] = {}
    for issue in issues:
        unique[(issue.code, issue.location, issue.candidate_id, issue.field_name)] = issue
    ordered = tuple(sorted(unique.values(), key=lambda item: (item.location, item.code, item.candidate_id or "", item.field_name or "")))
    status = FinalizationStatus.FAILED_VALIDATION if any(issue.hard for issue in ordered) else FinalizationStatus.REVIEW_REQUIRED if ordered else FinalizationStatus.READY_FOR_SEMANTIC_REVIEW
    return FinalizationReadiness(policy_version=FINALIZATION_POLICY_VERSION, status=status, issues=ordered)


def build_finalization_candidate(normalization_input: NormalizationInput, extraction: FinalOrderFormExtraction) -> tuple[FinalOrderFormExtraction, FinalizationReadiness]:
    """Return an unchanged-value final candidate and its deterministic readiness."""

    readiness = assess_finalization_readiness(normalization_input, extraction)
    # `COMPLETED` is intentionally never emitted by Stage 5A.
    output_status = ValidationStatus.FAILED_VALIDATION if readiness.status is FinalizationStatus.FAILED_VALIDATION else ValidationStatus.REVIEW_REQUIRED
    candidate = extraction.model_copy(update={"validation_status": output_status})
    return candidate, readiness
