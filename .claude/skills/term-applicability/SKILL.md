---
name: term-applicability
description: Decide whether each already-selected contract term applies document-wide, to specific product candidates, or remains unknown, and separately extract raw evidence-backed commercial facts (quantity, price, dates, payment terms, and similar) for existing purchased/included candidates, through narrow evidence tools. Use only after a prior triage pass has already selected the terms worth attribution — never for classifying document metadata, SKU selection, value normalization, or arithmetic.
---

# Term applicability

You are given one fixed batch: every product candidate, and the terms a
prior triage pass already selected as potentially commercially relevant —
administrative terms (billing contact, signature, vendor name, quote number,
and similar document metadata) have already been excluded from this batch
and are not your concern. You do not need to classify anything outside the
terms you were given. Their raw names, values, commercial statuses, and
evidence are already fixed and cannot be changed. Your job has two parts:
(1) deciding, for each term you were given, whether it applies to the whole
order form, to specific named candidates, or remains unknown, and (2)
extracting raw commercial facts for candidates that are already eligible.

Start from the compact term and candidate summaries you were given; do not
call an evidence tool for every term by default, and do not compare every
term against every candidate — most terms resolve from their own content and
immediate context, not from an exhaustive sweep.

Before deciding `document` or `candidate` scope for a term, call
`get_term_evidence_region` for that term at least once — an affirmative
scope claim without ever having retrieved that term's own evidence is not
grounded and will be rejected. `unknown` scope does not require a retrieval
call. Before naming any candidate in a `candidate`-scope decision, call
`get_candidate_evidence_region` for each named candidate at least once —
every named candidate needs its own confirmed retrieval, not just the term's.

Evidence IDs shown in task context are not automatically usable: submit an ID
only after its corresponding evidence-tool call succeeds in this run. Complete
all required retrievals before the first finalizer call. Every affirmative
document/candidate scope claim needs retrieved term evidence; candidate scope
also needs retrieved evidence for every named candidate; every raw commercial
fact needs retrieved evidence for that fact's candidate. If evidence cannot be
retrieved, omit the unsupported fact or claim or use `unknown` rather than
citing the unretrieved ID.

Assign `candidate` only when the term's own evidence directly links it to
one or more named product candidates, a product section, or
candidate-specific pricing/term context — and name every candidate it
applies to. Assign `document` only when the evidence explicitly establishes
agreement-wide, order-form-wide, or all-services applicability. Otherwise
assign `unknown`. `unknown` is a correct, preferred outcome whenever the
evidence does not safely establish either relationship — it is not a failure
to be minimized by guessing, and it is not license to invent a candidate
association merely because a term appears somewhere in the same document.

Every decision, including `unknown`, requires cited evidence: reference
evidence that already belongs to that term (or, for a candidate-scope
decision, evidence that grounds the term-to-candidate link), never evidence
you invented or evidence belonging to a different term, candidate, or
document.

Do not select a SKU, parse or normalize dates or money, calculate totals, or
reconsider a candidate's commercial status — none of that is this skill's
job. Never use direct filesystem, PostgreSQL/SQL, or Bash access, and do not
load the complete preprocessing artifact or the whole analysis result when
the compact batch you were given is sufficient.

## Raw commercial facts

Separately from term-applicability decisions, extract raw commercial facts
for each candidate below whose `commercial_status` is `purchased` or
`included` only — never for `excluded`, `optional`, `mentioned`, or
`ambiguous` candidates, and never for a candidate you would invent. This
enriches an existing candidate ID; it never discovers a new candidate, maps
a SKU, normalizes a value, calculates a total, or infers a missing value.

A fact's `field` is restricted to: `quantity`, `currency`, `unit_price`,
`unit_price_period`, `total_listed_value`, `service_start_date`,
`service_end_date`, `invoicing_schedule_type`, `invoicing_frequency`,
`payment_terms`, `special_note`, `yearly_price`. Before asserting any fact
for a candidate, call `get_candidate_evidence_region` for that candidate and
cite only that candidate's own evidence IDs already listed in the compact
context — never a generic search, and never evidence belonging to a
different candidate. Preserve every value exactly as raw text taken from the
document: never parse a date into ISO form, never parse money into a
decimal, never calculate a total, unit price, or quantity, and never
normalize a billing-schedule label into an enum of your own choosing —
downstream stages do that normalization from your raw text, not you.

A multi-year or multi-period value (Year 1, Year 2, Year 3, and similar)
must be reported as separate `yearly_price` facts distinguished by
`raw_period_label`, never summed, averaged, or collapsed into one value. A
candidate may otherwise report at most one fact per field unless distinct
period context (a period label or start/end pair) genuinely separates them,
or the field is `special_note` and the notes are genuinely distinct.

For every eligible candidate, `service_start_date`, `service_end_date`,
`invoicing_schedule_type`, `invoicing_frequency`, and `payment_terms` are
always expected, in addition to `quantity`, `unit_price`, and
`total_listed_value` whenever document analysis already hinted at them.
Actively retrieve that candidate's evidence and look for each expected
field -- do not extract only the fields document analysis happened to hint
at and skip the rest just because nothing pointed at them first.

Submit one coverage declaration per eligible candidate in
`candidate_commercial_fact_coverage`, listing only the expected fields you
could not extract, in `unresolved_fields`. Retrieve candidate evidence
before declaring a field unresolved; unresolved is an honest outcome and
requires that retrieved evidence. Do not restate which fields you did
extract, and do not submit `extracted_fields` yourself -- the application
derives that set directly from your own submitted `candidate_commercial_facts`
for that candidate, so summarizing it again is unnecessary and will be
rejected. Omit unsupported facts rather than inventing values, and use
`unknown` for unsupported term attribution.

## Completing the batch

Complete work only by calling `finalize_term_applicability` with the full
batch of decisions and any candidate commercial facts, grouped by
candidate ID; never use a prose final answer. Submit exactly one decision for
every selected term; use `unknown` when evidence cannot support attribution.
Submit evidence by ID only; the finalizer resolves
each ID to its exact, trusted evidence record — you do not need to (and
cannot) reconstruct a bounding box, representation, or extraction source,
and you never supply a fact's own identifier. Application-owned identifiers
(organization, document, preprocessing-run, analysis-run IDs, every version
label, and every fact's `fact_id`) are supplied for you and are not yours to
set or change. If the tool returns safe validation issues, correct only
those issues and resubmit at most once when it explicitly permits
correction.

This skill becomes active only when the Claude Agent SDK configuration
enables project skill loading.
