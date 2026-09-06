---
name: order-form-analysis
description: Analyze a persisted order-form preprocessing run through narrow read-only document tools. Use when identifying contract structure, pricing, terms, product candidates, commercial treatment, and supporting evidence without selecting SKUs.
---

# Order-form analysis

Start with `get_document_overview`. Identify likely contract, pricing, product, and
terms sections, then use `search_document` before loading substantial content.
Retrieve only relevant page text, blocks, and tables. Request a page render only
when persisted text and layout cannot resolve visible structure. Connect facts across
pages when necessary.

Create document-level contract structure and global terms. Create product candidates
for document-described products or services, then separately classify commercial
status as purchased, included, optional, excluded, mentioned, or ambiguous.

Attach precise persisted evidence to every material conclusion, including negative
or non-purchased classifications. Preserve raw semantic values for later deterministic
normalization. Return ambiguity when evidence is incomplete, conflicting, or cannot
support one classification. Complete work only by calling
`finalize_document_analysis` with the full semantic result; never use a prose final
answer. If the tool returns safe validation issues, correct only those issues and
resubmit at most once when it explicitly permits correction.

A search with no results is not proof that a fact or product is absent. Search using
reasonable document terminology and inspect likely sections before concluding that
information is missing.

Treat native text, OCR text, tables, layout blocks, and page renders as separate
representations of the same source document. Do not silently combine conflicting
values. When representations conflict, inspect the page render if available, preserve
the conflicting evidence, and return an ambiguous conclusion when it cannot be
resolved reliably.

Never select a final SKU, invent catalog data, perform money arithmetic, silently
normalize dates or money, or make unsupported conclusions. Do not treat examples,
mentions, exclusions, or optional products as purchases without evidence. Never use
direct filesystem, PostgreSQL/SQL, or Bash access, and do not load the complete
preprocessing artifact when narrow retrieval is sufficient.

This skill becomes active only when the Claude Agent SDK configuration enables
project skill loading.
