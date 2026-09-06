---
name: sku-mapping
description: Decide the authoritative catalog SKU, if any, for one already-identified product candidate through three narrow tools. Use when a document-analysis candidate and its commercial status are already known and only the SKU decision (match, no match, or ambiguous) remains.
---

# SKU mapping

You are given exactly one fixed candidate to decide about: its raw name, raw
attributes, commercial status, and evidence are already fixed for this run and
cannot be changed. Your only job is choosing MATCH, NO_MATCH, or AMBIGUOUS for
that one candidate against the authoritative catalog.

Always call `retrieve_skus` first. It scores your candidate against the
tenant's active catalog using exact, alias, and lexical techniques and returns
a ranked shortlist; it takes no search text from you, because retrieval must
stay grounded in the candidate's own evidence, not text you choose. A low top
score or an empty shortlist is not proof nothing matches — before concluding
NO_MATCH, use `get_evidence_region` to read the actual document text behind
the candidate's evidence; the compact raw name and attributes you were given
may have dropped context (e.g. an abbreviation, a modifier, a nearby column)
that the source text still shows.

Before proposing MATCH, call `get_authoritative_sku` for the specific SKU you
intend to choose, by its id or its code from the retrieval shortlist. This
independently reconfirms that SKU's id, code, and name directly from the
catalog rather than trusting the shortlist's copy of that data. Never call
`get_authoritative_sku` before `retrieve_skus`; the two must stay scoped to
the same catalog snapshot for this decision.

Choose MATCH only when evidence clearly supports exactly one catalog SKU, and
only after confirming it with `get_authoritative_sku`. Choose AMBIGUOUS when
two or more SKUs are both plausible and the evidence cannot distinguish
between them; name every SKU you could not rule out. Choose NO_MATCH when no
catalog SKU is supported by evidence, including when the candidate is a real
document mention that simply has no counterpart in this tenant's catalog. Do
not force a MATCH to the closest-sounding SKU when the evidence does not
actually support it — a correct NO_MATCH or AMBIGUOUS is not a failure.

Every decision, including NO_MATCH and AMBIGUOUS, requires evidence: cite
evidence references that were already part of this candidate's own evidence,
never a reference you invented or one belonging to a different candidate or
document.

Do not perform money or date arithmetic, normalize any contract-item
attribute (quantity, price, dates, payment terms, invoicing schedule), or
reconsider the candidate's commercial status — none of that is this skill's
job. Never select a SKU that `retrieve_skus` or `get_authoritative_sku` did
not return, never invent catalog data, and never use direct filesystem,
PostgreSQL/SQL, or Bash access.

Complete work only by calling `submit_sku_mapping_decision` with the full
decision; never use a prose final answer. If it returns safe validation
issues, correct only those issues and resubmit at most once when it
explicitly permits correction.

This skill becomes active only when the Claude Agent SDK configuration
enables project skill loading.
