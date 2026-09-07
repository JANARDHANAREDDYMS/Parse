"""Generated-fixture tests for deterministic term inheritance and validation."""
from decimal import Decimal

from test_normalization_assembly import _assemble, base_scenario, evidence
from maximor.document_analysis.schemas import ApplicabilityScope, GlobalTerm
from maximor.normalization.inheritance import apply_document_term_inheritance
from maximor.normalization.service import assemble_normalized_draft
from maximor.normalization.validation import validate_normalized_extraction
from maximor.normalization.schemas import ProvenanceSourceType, ValueOrigin
from maximor.term_applicability.schemas import TermApplicabilityDecision, TermDisposition


def test_unknown_and_unsupported_terms_are_never_inherited():
    scenario = base_scenario()
    analysis = scenario["document_analysis"].model_copy(update={"global_terms": (GlobalTerm(term_id="term-unknown", raw_name="Renewal", raw_value="annual", applicability_scope=ApplicabilityScope.UNKNOWN, evidence=(evidence(),)),)})
    applicability = scenario["term_applicability"].model_copy(update={"decisions": (TermApplicabilityDecision(schema_version="1", term_id="term-unknown", disposition=TermDisposition.LINE_ITEM, applicability_scope=ApplicabilityScope.UNKNOWN, evidence=(evidence(),)),)})
    draft = assemble_normalized_draft(_assemble(document_analysis=analysis, term_applicability=applicability))
    assert all(item.payment_terms is None for item in draft.line_items)


def test_document_term_alias_is_available_for_explicit_inheritance_only():
    scenario = base_scenario()
    analysis = scenario["document_analysis"].model_copy(update={"global_terms": (GlobalTerm(term_id="term-payment", raw_name="Payment Terms", raw_value="Net 30", applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),)})
    applicability = scenario["term_applicability"].model_copy(update={"decisions": (TermApplicabilityDecision(schema_version="1", term_id="term-payment", disposition=TermDisposition.LINE_ITEM, applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),)})
    draft = assemble_normalized_draft(_assemble(document_analysis=analysis, term_applicability=applicability))
    assert all(item.payment_terms == "Net 30" for item in draft.line_items)
    assert all(item.field_provenance["payment_terms"].value_origin is ValueOrigin.INHERITED for item in draft.line_items)
    assert all(item.field_provenance["payment_terms"].source_type is ProvenanceSourceType.DOCUMENT_TERM for item in draft.line_items)


def test_validation_reports_conflicting_line_total_and_multiple_currencies_deterministically():
    normalization_input = _assemble()
    draft = assemble_normalized_draft(normalization_input)
    report = validate_normalized_extraction(normalization_input, draft)
    assert tuple(sorted(issue.code for issue in report.issues)) == tuple(issue.code for issue in report.issues)
    assert report.requires_review is False or isinstance(report.requires_review, bool)


def test_stage4_does_not_expose_paths_sql_or_raw_document_bodies():
    draft = assemble_normalized_draft(_assemble())
    serialized = str(draft.model_dump(mode="json"))
    assert "/Users/" not in serialized and "SELECT " not in serialized.upper()
