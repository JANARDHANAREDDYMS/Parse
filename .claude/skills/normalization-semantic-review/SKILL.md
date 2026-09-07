---
name: normalization-semantic-review
description: Review only explicitly assigned deterministic normalization ambiguities and return bounded evidence-backed findings.
---

# Normalization semantic review

Review only the assigned `review_item_id` values. Start by reading each
assigned context, then retrieve the allowed evidence IDs before making an
affirmative interpretation. Finalize exactly one finding per assigned item
through `finalize_normalization_semantic_review`, with `findings` submitted
in ascending lexical order by `review_item_id` and no duplicates -- an
out-of-order or duplicate submission is rejected before anything else is
checked.

Prefer `INSUFFICIENT_EVIDENCE` or `CONFLICT_UNRESOLVED` over guessing. Never
calculate money, derive totals, normalize dates or quantities, change/select a
SKU, create a candidate, or access files, SQL, storage paths, Bash, web, or
unassigned candidates/terms. A correction route may name only its owning
upstream stage and must cite evidence retrieved in this run. Do not send
deterministic missing/unsupported/arithmetic/integrity issues to semantic
review. Finalize promptly once every assigned item is addressed.

This skill is active only when the future Agent SDK configuration enables
project skill loading.
