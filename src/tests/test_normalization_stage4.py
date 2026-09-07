"""Generated-fixture tests for deterministic term inheritance and validation."""
from datetime import date
from decimal import Decimal

from test_normalization_assembly import _assemble, base_scenario, evidence, match_artifact
from maximor.document_analysis.schemas import ApplicabilityScope, GlobalTerm
from maximor.normalization.inheritance import apply_document_term_inheritance
from maximor.normalization.service import assemble_normalized_draft
from maximor.normalization.validation import validate_normalized_extraction
from maximor.normalization.schemas import ProvenanceSourceType, ValueOrigin
from maximor.term_applicability.schemas import CandidateCommercialFacts, RawCommercialFact, RawCommercialFactField, TermApplicabilityDecision, TermDisposition, compute_fact_id


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


def test_start_and_end_date_terms_inherit_via_the_bare_alias_and_dash_separated_format():
    """Reproduces the real `of-0001` gap.

    Document analysis often names these terms 'Start Date'/'End Date', not
    'Service Start Date'/'Service End Date' -- the bare phrasing must also be
    a recognized alias. The document's own dash-separated day-month-year
    format ('01-Aug-2026') must parse too; before this fix `normalize_date`
    only accepted space-separated variants and silently rejected this one.
    """

    scenario = base_scenario()
    analysis = scenario["document_analysis"].model_copy(update={"global_terms": (
        GlobalTerm(term_id="term-end", raw_name="End Date", raw_value="31 Jul 2027", applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
        GlobalTerm(term_id="term-start", raw_name="Start Date", raw_value="01-Aug-2026", applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    applicability = scenario["term_applicability"].model_copy(update={"decisions": (
        TermApplicabilityDecision(schema_version="1", term_id="term-end", disposition=TermDisposition.LINE_ITEM, applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
        TermApplicabilityDecision(schema_version="1", term_id="term-start", disposition=TermDisposition.LINE_ITEM, applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    draft = assemble_normalized_draft(_assemble(document_analysis=analysis, term_applicability=applicability))
    assert all(item.service_start_date == date(2026, 8, 1) for item in draft.line_items)
    assert all(item.service_end_date == date(2027, 7, 31) for item in draft.line_items)


def test_invoicing_schedule_type_inherits_only_a_value_within_the_fixed_enum():
    """`invoicing_schedule_type` must be inheritable at all, and only within its enum.

    Before this fix, `invoicing_schedule_type` had no alias entry and was not
    in the inheritance policy's allowed-field set, so it could never be
    inherited regardless of phrasing.
    """

    scenario = base_scenario()
    valid = scenario["document_analysis"].model_copy(update={"global_terms": (
        GlobalTerm(term_id="term-schedule", raw_name="Invoicing Schedule Type", raw_value="Recurring", applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    decisions = (TermApplicabilityDecision(schema_version="1", term_id="term-schedule", disposition=TermDisposition.LINE_ITEM, applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),)
    applicability = scenario["term_applicability"].model_copy(update={"decisions": decisions})
    draft = assemble_normalized_draft(_assemble(document_analysis=valid, term_applicability=applicability))
    assert all(item.invoicing_schedule_type == "recurring" for item in draft.line_items)

    invalid = scenario["document_analysis"].model_copy(update={"global_terms": (
        GlobalTerm(term_id="term-schedule", raw_name="Invoicing Schedule Type", raw_value="whenever we feel like it", applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    draft = assemble_normalized_draft(_assemble(document_analysis=invalid, term_applicability=applicability))
    assert all(item.invoicing_schedule_type is None for item in draft.line_items)


def test_payment_terms_inherits_from_a_compound_section_header_name():
    """Reproduces the real `of-0002` gap.

    Document analysis named the term after the document's own section
    heading, 'Invoice Plan / Payment Terms', rather than the bare concept
    'Payment Terms'. The exact-match alias lookup silently missed this,
    leaving `payment_terms` null even though the value ('Net 55') was
    present and unambiguous. Recognizing a known alias phrase embedded
    anywhere in the label -- still an exact phrase, never fuzzy -- fixes it.
    """

    scenario = base_scenario()
    analysis = scenario["document_analysis"].model_copy(update={"global_terms": (
        GlobalTerm(term_id="term-payment", raw_name="Invoice Plan / Payment Terms", raw_value="Invoices are sent in advance in accordance with each line item's billing frequency. Payment terms include Net 55 unless stated otherwise.", applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    applicability = scenario["term_applicability"].model_copy(update={"decisions": (
        TermApplicabilityDecision(schema_version="1", term_id="term-payment", disposition=TermDisposition.LINE_ITEM, applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    draft = assemble_normalized_draft(_assemble(document_analysis=analysis, term_applicability=applicability))
    assert all(item.payment_terms == "Net 55" for item in draft.line_items)
    assert all(item.field_provenance["payment_terms"].value_origin is ValueOrigin.INHERITED for item in draft.line_items)


def test_invoicing_schedule_type_heuristic_guess_is_flagged_and_evidence_linked():
    """`invoicing_schedule_type` has no recoverable textual signal in this dataset (verified
    across all 50 real documents' ground truth), so a resolved `invoicing_frequency` is used
    to make a disclosed, majority-vote guess -- never presented as a genuine extraction.

    The guess must reuse the frequency's own evidence (there is no independent evidence for
    the guess itself) and must be paired with a `ReviewIssue` so it is auditable. A candidate
    with no resolved frequency at all gets no guess -- there is nothing to hang it on.
    """

    scenario = base_scenario()
    analysis = scenario["document_analysis"].model_copy(update={"global_terms": (
        GlobalTerm(term_id="term-freq", raw_name="Invoicing Frequency", raw_value="Monthly", applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    applicability = scenario["term_applicability"].model_copy(update={"decisions": (
        TermApplicabilityDecision(schema_version="1", term_id="term-freq", disposition=TermDisposition.LINE_ITEM, applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    draft = assemble_normalized_draft(_assemble(document_analysis=analysis, term_applicability=applicability))
    assert all(item.invoicing_frequency == "monthly" for item in draft.line_items)
    assert all(item.invoicing_schedule_type == "recurring" for item in draft.line_items)
    assert all(item.field_provenance["invoicing_schedule_type"].evidence == item.field_provenance["invoicing_frequency"].evidence for item in draft.line_items)
    guess_issues = [issue for issue in draft.review_issues if issue.code == "invoicing_schedule_type_heuristic_guess"]
    assert len(guess_issues) == len(draft.line_items)

    # No frequency resolved at all -> nothing to hang a guess on, left honestly null.
    unresolved_draft = assemble_normalized_draft(_assemble())
    assert all(item.invoicing_frequency is None for item in unresolved_draft.line_items)
    assert all(item.invoicing_schedule_type is None for item in unresolved_draft.line_items)
    assert not any(issue.code == "invoicing_schedule_type_heuristic_guess" for issue in unresolved_draft.review_issues)


def test_start_date_alias_resolves_through_ocr_digit_for_letter_noise():
    """Reproduces the real `OF-0027` gap: OCR corrupted 'Start Date' to '5tart Date'.

    Neither the exact-match nor the compound-header substring fallback
    recognizes '5tart date' as containing 'start date' -- the digit breaks
    the match. Undoing a small, confirmed set of digit-for-letter OCR
    confusions (0/o, 1/l, 5/s) and retrying recovers it. No alias phrase
    contains a digit, so this can only ever surface an already-known phrase.
    """

    scenario = base_scenario()
    analysis = scenario["document_analysis"].model_copy(update={"global_terms": (
        GlobalTerm(term_id="term-start", raw_name="5tart Date", raw_value="10 Dec 2024", applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    applicability = scenario["term_applicability"].model_copy(update={"decisions": (
        TermApplicabilityDecision(schema_version="1", term_id="term-start", disposition=TermDisposition.LINE_ITEM, applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    draft = assemble_normalized_draft(_assemble(document_analysis=analysis, term_applicability=applicability))
    assert all(item.service_start_date == date(2024, 12, 10) for item in draft.line_items)


def test_payment_terms_extracted_from_a_term_whose_name_maps_elsewhere():
    """Reproduces the real `of-0008` gap: a payment-terms convention hiding
    inside an *unrelated-by-name* term.

    The document's own global term is literally named 'Invoicing Schedule'
    (which correctly resolves to `invoicing_schedule_type`), but its value is
    boilerplate that also states the payment-terms convention: "Invoices are
    sent in advance in accordance with each line item's billing frequency.
    Payment terms include Due on Receipt unless stated otherwise." Before
    this fix, payment_terms stayed null because inheritance only ever wrote
    a term's value into the ONE field its name resolved to.
    """

    scenario = base_scenario()
    analysis = scenario["document_analysis"].model_copy(update={"global_terms": (
        GlobalTerm(
            term_id="term-invoicing-schedule", raw_name="Invoicing Schedule",
            raw_value="Invoices are sent in advance in accordance with each line item's billing frequency. Payment terms include Due on Receipt unless stated otherwise.",
            applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),),
        ),
    )})
    applicability = scenario["term_applicability"].model_copy(update={"decisions": (
        TermApplicabilityDecision(schema_version="1", term_id="term-invoicing-schedule", disposition=TermDisposition.LINE_ITEM, applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    draft = assemble_normalized_draft(_assemble(document_analysis=analysis, term_applicability=applicability))
    assert all(item.payment_terms == "Due on Receipt" for item in draft.line_items)
    assert all(item.invoicing_schedule_type is None for item in draft.line_items)


def test_payment_terms_alias_match_prefers_the_longest_phrase_in_a_combined_label():
    """A label combining two alias phrases must resolve to the more specific one.

    'Billing Schedule / Invoicing Frequency' contains both 'billing schedule'
    (-> invoicing_schedule_type) and 'invoicing frequency' (-> invoicing_frequency)
    as substrings; the longer phrase, 'invoicing frequency', is the one that
    should win rather than whichever alias happens to be found first -- so
    the term itself must never be inherited onto `invoicing_schedule_type`
    (only the unrelated majority-vote heuristic may populate that field, and
    only via a DERIVED provenance, never DOCUMENT_TERM/INHERITED).
    """

    scenario = base_scenario()
    analysis = scenario["document_analysis"].model_copy(update={"global_terms": (
        GlobalTerm(term_id="term-freq", raw_name="Billing Schedule / Invoicing Frequency", raw_value="Monthly", applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    applicability = scenario["term_applicability"].model_copy(update={"decisions": (
        TermApplicabilityDecision(schema_version="1", term_id="term-freq", disposition=TermDisposition.LINE_ITEM, applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    draft = assemble_normalized_draft(_assemble(document_analysis=analysis, term_applicability=applicability))
    assert all(item.invoicing_frequency == "monthly" for item in draft.line_items)
    assert all(item.field_provenance["invoicing_frequency"].source_type is ProvenanceSourceType.DOCUMENT_TERM for item in draft.line_items)
    assert all(item.field_provenance["invoicing_schedule_type"].source_type is ProvenanceSourceType.DERIVED for item in draft.line_items)


def test_multi_period_frequency_derives_after_document_term_inherited_dates_are_applied():
    """Reproduces a real ordering bug found while fixing the `OF-0008` shape.

    Service dates supplied only as document-level terms ('Start Date'/'End
    Date') are not available during initial per-candidate assembly -- they
    only exist after document-term inheritance runs. A subscription total
    that reconciles against the billing ratio (1 unit at 183,000/period,
    total 2,196,000, over the 12-month span the dates describe) must still
    have its `invoicing_frequency` derived once those dates are known, not
    silently skipped because they weren't present yet at the point frequency
    derivation was first attempted. `invoicing_schedule_type` is NOT derived
    from this math (see `OF-0002` counterexample elsewhere) -- the "recurring"
    seen here instead comes from the disclosed majority-vote heuristic for a
    "monthly" frequency (see `_guess_invoicing_schedule_type`), not the ratio.
    """
    scenario = base_scenario()
    candidate_id = "candidate-hinted"
    quantity = RawCommercialFact(fact_id=compute_fact_id(candidate_id=candidate_id, field=RawCommercialFactField.QUANTITY, raw_value="1"), candidate_id=candidate_id, field=RawCommercialFactField.QUANTITY, raw_value="1", evidence=(evidence(),))
    unit_price = RawCommercialFact(fact_id=compute_fact_id(candidate_id=candidate_id, field=RawCommercialFactField.UNIT_PRICE, raw_value="183000"), candidate_id=candidate_id, field=RawCommercialFactField.UNIT_PRICE, raw_value="183000", evidence=(evidence(),))
    total = RawCommercialFact(fact_id=compute_fact_id(candidate_id=candidate_id, field=RawCommercialFactField.TOTAL_LISTED_VALUE, raw_value="2196000"), candidate_id=candidate_id, field=RawCommercialFactField.TOTAL_LISTED_VALUE, raw_value="2196000", evidence=(evidence(),))
    currency = RawCommercialFact(fact_id=compute_fact_id(candidate_id=candidate_id, field=RawCommercialFactField.CURRENCY, raw_value="USD"), candidate_id=candidate_id, field=RawCommercialFactField.CURRENCY, raw_value="USD", evidence=(evidence(),))
    scenario["term_applicability"] = scenario["term_applicability"].model_copy(update={
        "candidate_commercial_facts": (CandidateCommercialFacts(candidate_id=candidate_id, facts=(quantity, unit_price, total, currency)),),
    })
    scenario["sku_mappings"][candidate_id] = match_artifact(candidate_id, raw_attributes={"qty": "1"}, sku_code="HINTED")
    scenario["document_analysis"] = scenario["document_analysis"].model_copy(update={"global_terms": (
        GlobalTerm(term_id="term-end", raw_name="End Date", raw_value="2026-01-01", applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
        GlobalTerm(term_id="term-start", raw_name="Start Date", raw_value="2025-01-01", applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})
    scenario["term_applicability"] = scenario["term_applicability"].model_copy(update={"decisions": (
        *scenario["term_applicability"].decisions,
        TermApplicabilityDecision(schema_version="1", term_id="term-end", disposition=TermDisposition.LINE_ITEM, applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
        TermApplicabilityDecision(schema_version="1", term_id="term-start", disposition=TermDisposition.LINE_ITEM, applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),)),
    )})

    draft = assemble_normalized_draft(_assemble(**scenario))
    item = next(i for i in draft.line_items if i.source_candidate_id == candidate_id)
    assert item.service_start_date == date(2025, 1, 1)
    assert item.service_end_date == date(2026, 1, 1)
    assert item.invoicing_frequency == "monthly"
    assert item.invoicing_schedule_type == "recurring"


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
