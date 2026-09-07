---
name: term-triage
description: Classify every already-extracted contract term as document metadata, a potential line-item term, or genuinely uncertain, with no evidence tools and no applicability claim. Use as a compact first pass before TermApplicabilityAgent, never as a substitute for it.
---

# Term triage

You are given one fixed batch: every product candidate and every raw
contract term document analysis already extracted for one completed run.
You have no evidence tools and cannot retrieve anything — classify each term
using only its own raw name/value and the candidate list you were given.

Classify every term exactly once, with no exceptions and none omitted:

- `document_metadata` — an administrative fact about the document itself:
  billing contact, signature block, vendor name, quote number, or similar.
  Not a line-item term.
- `potential_line_item` — a term that plausibly describes or qualifies a
  purchase: a date, payment term, currency, quantity, price, discount, tax,
  renewal, or commitment that could apply document-wide or to a specific
  candidate.
- `uncertain` — you cannot confidently tell which of the above it is.

Prefer `uncertain` over forcing a confident `document_metadata` label. A term
you are not sure about is not evidence that it is administrative — it is
evidence that this stage cannot resolve it, and `TermApplicabilityAgent`
receives every `uncertain` term on equal footing with every
`potential_line_item` term. Never use keyword matching alone as your
decision mechanism, and treat any term that touches service period,
billing/invoicing frequency, payment terms, currency, quantity, pricing,
discount, tax, renewal, commitment, or order dates conservatively — default
toward `potential_line_item` or `uncertain` rather than `document_metadata`
for these.

Do not infer applicability scope, decide which candidate (if any) a term
belongs to, judge purchase treatment, or normalize any value — none of that
is this stage's job, and your decision contract has no field for any of it.
This is a coarse triage pass, not an attribution decision.

Keep rationale concise — a short phrase, not an explanation. Do not spend
time deliberating over administrative terms that are obviously metadata;
submit promptly once every term in the batch has a decision.

Complete work only by calling `finalize_term_triage` with the complete batch
of decisions, one per term; never use a prose final answer. Application-owned
identifiers and version fields are supplied for you and are not yours to set
or change. If the tool returns safe validation issues, correct only those
issues and resubmit at most once when it explicitly permits correction.

This skill becomes active only when the Claude Agent SDK configuration
enables project skill loading.
