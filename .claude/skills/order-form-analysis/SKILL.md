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
status: purchased, included, optional, excluded, mentioned, or ambiguous. Attach
precise persisted evidence to every material conclusion. Return ambiguity when the
evidence is incomplete. Preserve raw semantic values for later deterministic
normalization.

Never select a final SKU, invent catalog data, perform money arithmetic, silently
normalize dates or money, or make unsupported conclusions. Do not treat examples,
mentions, exclusions, or optional products as purchases without evidence. Never use
direct filesystem, PostgreSQL/SQL, or Bash access, and do not load the complete
preprocessing artifact when narrow retrieval is sufficient.

This skill becomes active only when a future Claude Agent SDK configuration enables
project skill loading. That configuration is intentionally not included here.
