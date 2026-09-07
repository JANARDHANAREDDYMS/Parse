"""Field-specific inheritance of trusted document terms by exact alias phrase.

The policy receives a trusted input and provisional line items. It recognizes
only exact, known alias phrases (optionally embedded in a compound section
header) -- never fuzzy or semantic similarity -- and never mutates upstream
objects.
"""
from maximor.document_analysis.schemas import ApplicabilityScope
from maximor.normalization.contracts import NormalizationInput
from maximor.normalization.normalizers import INVOICING_FREQUENCIES, INVOICING_SCHEDULE_TYPES, extract_payment_terms_mention, normalize_billing_enum, normalize_currency, normalize_date, normalize_payment_terms
from maximor.normalization.schemas import FieldProvenance, FinalOrderFormExtraction, NormalizedOrderMetadata, ProvenanceSourceType, ReviewIssue, ReviewIssueSeverity, ValueOrigin
from maximor.normalization.versions import INHERITANCE_POLICY_VERSION

ALIASES = {
    "payment terms": "payment_terms", "payment term": "payment_terms",
    "invoice frequency": "invoicing_frequency", "invoicing frequency": "invoicing_frequency", "billing frequency": "invoicing_frequency",
    "invoicing schedule type": "invoicing_schedule_type", "invoicing schedule": "invoicing_schedule_type",
    "billing schedule type": "invoicing_schedule_type", "billing schedule": "invoicing_schedule_type",
    "effective date": "effective_date", "order form effective date": "effective_date",
    "service start date": "service_start_date", "service end date": "service_end_date",
    "start date": "service_start_date", "end date": "service_end_date",
    "currency": "currency", "order currency": "currency",
}

def _label(value: str) -> str:
    return " ".join(value.casefold().split())

def _match_alias(label: str) -> str | None:
    """Match a label against `ALIASES`, exact first, then longest-substring."""
    if label in ALIASES:
        return ALIASES[label]
    matches = [(phrase, field) for phrase, field in ALIASES.items() if phrase in label]
    if not matches:
        return None
    return max(matches, key=lambda pair: len(pair[0]))[1]

# A real `OF-0027` term is named "5tart Date" -- OCR substituting a digit for
# a similar-looking letter. Undoing only this specific, confirmed confusion
# set (never a broader fuzzy match) is safe here because no alias phrase in
# `ALIASES` contains a digit, so this substitution can only ever surface an
# already-known phrase, never invent a new unintended match.
_OCR_DIGIT_LETTER_SUBSTITUTIONS = str.maketrans({"0": "o", "1": "l", "5": "s"})

def _resolve_alias(label: str) -> str | None:
    """Resolve a term's field alias, tolerating a compound header or OCR noise.

    Document analysis sometimes names a term after the document's own
    section heading rather than the bare concept -- "Invoice Plan / Payment
    Terms" instead of "Payment Terms" -- so an exact match against the full
    label misses real, unambiguous terms. Recognizing a known alias phrase
    appearing anywhere in the label (preferring the longest match, so a
    combined header is never misassigned to a shorter alias it happens to
    also contain) still requires an exact phrase, never fuzzy similarity.
    Only after that fails is a small set of confirmed OCR digit-for-letter
    substitutions undone and retried, for the same reason.
    """

    match = _match_alias(label)
    if match is not None:
        return match
    denoised = label.translate(_OCR_DIGIT_LETTER_SUBSTITUTIONS)
    if denoised == label:
        return None
    return _match_alias(denoised)

def _provenance(term, *, inherited: bool) -> FieldProvenance:
    return FieldProvenance(source_type=ProvenanceSourceType.DOCUMENT_TERM, value_origin=ValueOrigin.INHERITED if inherited else ValueOrigin.EXTRACTED, source_term_id=term.term_id, evidence=term.evidence)

def apply_document_term_inheritance(normalization_input: NormalizationInput, draft: FinalOrderFormExtraction) -> FinalOrderFormExtraction:
    """Apply only exact supported aliases with direct/candidate/document precedence."""
    terms = {t.term_id: t for t in normalization_input.document_analysis.global_terms}
    decisions = {d.term_id: d for d in normalization_input.term_applicability.decisions}
    supported = []
    mentionable = []
    for term_id, decision in decisions.items():
        term = terms.get(term_id)
        if term is None or decision.applicability_scope not in {ApplicabilityScope.DOCUMENT, ApplicabilityScope.CANDIDATE}:
            continue
        if not term.raw_value:
            continue
        field = _resolve_alias(_label(term.raw_name))
        if field:
            supported.append((term, decision, field))
        mentionable.append((term, decision))
    items = []
    for item in draft.line_items:
        updates, provenance = {}, dict(item.field_provenance)
        candidates = [(t, d, f) for t, d, f in supported if d.applicability_scope is ApplicabilityScope.CANDIDATE and item.source_candidate_id in d.applies_to_candidate_ids]
        docs = [(t, d, f) for t, d, f in supported if d.applicability_scope is ApplicabilityScope.DOCUMENT]
        for term, decision, field in [*candidates, *docs]:
            if field not in {"payment_terms", "invoicing_frequency", "invoicing_schedule_type", "service_start_date", "service_end_date", "currency"} or getattr(item, field, None) is not None:
                continue
            try:
                if field == "currency":
                    value = normalize_currency(term.raw_value)
                elif field in {"service_start_date", "service_end_date"}:
                    value = normalize_date(term.raw_value)
                elif field == "invoicing_frequency":
                    value = normalize_billing_enum(term.raw_value, allowed=INVOICING_FREQUENCIES)
                elif field == "invoicing_schedule_type":
                    value = normalize_billing_enum(term.raw_value, allowed=INVOICING_SCHEDULE_TYPES)
                else:
                    value = normalize_payment_terms(term.raw_value)
            except Exception:
                continue
            updates[field] = value; provenance[field] = _provenance(term, inherited=True)
            if candidates:
                break
        if item.payment_terms is None and "payment_terms" not in updates:
            mention_candidates = [(t, d) for t, d in mentionable if d.applicability_scope is ApplicabilityScope.CANDIDATE and item.source_candidate_id in d.applies_to_candidate_ids]
            mention_docs = [(t, d) for t, d in mentionable if d.applicability_scope is ApplicabilityScope.DOCUMENT]
            for term, decision in [*mention_candidates, *mention_docs]:
                mention = extract_payment_terms_mention(term.raw_value)
                if mention is not None:
                    updates["payment_terms"] = mention; provenance["payment_terms"] = _provenance(term, inherited=True)
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
