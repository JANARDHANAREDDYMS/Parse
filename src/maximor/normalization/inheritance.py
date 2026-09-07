"""Exact-match, field-specific inheritance of trusted document terms.

The policy receives a trusted input and provisional line items. It applies no
fuzzy matching or semantic inference, and never mutates upstream objects.
"""
from maximor.document_analysis.schemas import ApplicabilityScope
from maximor.normalization.contracts import NormalizationInput
from maximor.normalization.normalizers import normalize_currency, normalize_date, normalize_payment_terms
from maximor.normalization.schemas import FieldProvenance, FinalOrderFormExtraction, NormalizedOrderMetadata, ProvenanceSourceType, ReviewIssue, ReviewIssueSeverity, ValueOrigin
from maximor.normalization.versions import INHERITANCE_POLICY_VERSION

ALIASES = {
    "payment terms": "payment_terms", "payment term": "payment_terms",
    "invoice frequency": "invoicing_frequency", "invoicing frequency": "invoicing_frequency", "billing frequency": "invoicing_frequency",
    "effective date": "effective_date", "order form effective date": "effective_date",
    "service start date": "service_start_date", "service end date": "service_end_date",
    "currency": "currency", "order currency": "currency",
}

def _label(value: str) -> str:
    return " ".join(value.casefold().split())

def _provenance(term, *, inherited: bool) -> FieldProvenance:
    return FieldProvenance(source_type=ProvenanceSourceType.DOCUMENT_TERM, value_origin=ValueOrigin.INHERITED if inherited else ValueOrigin.EXTRACTED, source_term_id=term.term_id, evidence=term.evidence)

def apply_document_term_inheritance(normalization_input: NormalizationInput, draft: FinalOrderFormExtraction) -> FinalOrderFormExtraction:
    """Apply only exact supported aliases with direct/candidate/document precedence."""
    terms = {t.term_id: t for t in normalization_input.document_analysis.global_terms}
    decisions = {d.term_id: d for d in normalization_input.term_applicability.decisions}
    supported = []
    for term_id, decision in decisions.items():
        term = terms.get(term_id)
        if term is None or decision.applicability_scope not in {ApplicabilityScope.DOCUMENT, ApplicabilityScope.CANDIDATE}:
            continue
        field = ALIASES.get(_label(term.raw_name))
        if field and term.raw_value:
            supported.append((term, decision, field))
    items = []
    for item in draft.line_items:
        updates, provenance = {}, dict(item.field_provenance)
        candidates = [(t, d, f) for t, d, f in supported if d.applicability_scope is ApplicabilityScope.CANDIDATE and item.source_candidate_id in d.applies_to_candidate_ids]
        docs = [(t, d, f) for t, d, f in supported if d.applicability_scope is ApplicabilityScope.DOCUMENT]
        for term, decision, field in [*candidates, *docs]:
            if field not in {"payment_terms", "invoicing_frequency", "service_start_date", "service_end_date", "currency"} or getattr(item, field, None) is not None:
                continue
            try:
                value = normalize_currency(term.raw_value) if field == "currency" else normalize_date(term.raw_value) if field in {"service_start_date", "service_end_date"} else normalize_payment_terms(term.raw_value)
            except Exception:
                continue
            updates[field] = value; provenance[field] = _provenance(term, inherited=True)
            if candidates:
                break
        items.append(item.model_copy(update={**updates, "field_provenance": provenance}))
    metadata = draft.order_metadata or NormalizedOrderMetadata(schema_version=draft.schema_version)
    m_updates, m_prov = {}, dict(metadata.field_provenance)
    for term, decision, field in supported:
        if decision.applicability_scope is not ApplicabilityScope.DOCUMENT or field not in {"effective_date", "currency"} or getattr(metadata, field, None) is not None:
            continue
        try:
            value = normalize_currency(term.raw_value) if field == "currency" else normalize_date(term.raw_value)
        except Exception:
            continue
        m_updates[field] = value; m_prov[field] = _provenance(term, inherited=False)
    metadata = metadata.model_copy(update={**m_updates, "field_provenance": m_prov})
    return draft.model_copy(update={"order_metadata": metadata, "line_items": tuple(items), "review_issues": tuple(draft.review_issues)})
