# Term-triage subsystem

A functional, tool-free Claude classification pass that runs **before**
`TermApplicabilityAgent`. No persistence, migration, job type, worker
handler, API endpoint, or pipeline/scheduling wiring exists yet — nothing
here is reachable from a live upload, and nothing in this package makes a
paid Claude request except when a caller explicitly constructs
`ClaudeTermTriageAgent` and calls `execute`/`triage` with a live API key.

## Why this exists

A live smoke test of `TermApplicabilityAgent` against a real 28-term document
showed the agent spending most of its budget reasoning over every term,
including administrative ones (billing contact, signatures, quote numbers)
that were never going to resolve to a candidate. `TermTriageAgent` is a
cheap, compact, tool-free first pass that narrows the batch
`TermApplicabilityAgent` actually reasons over, without ever deleting,
normalizing, or making an applicability claim about any term itself.

## Components

| Component | Status | Input | Output |
|---|---|---|---|
| `TermTriageTask` / `build_term_triage_task` | **Implemented** | one already-validated `DocumentAnalysisResult` + `analysis_run_id` | `TermTriageTask` |
| `TermTriageDecision` / `TermTriageResult` / `build_term_triage_result` | **Implemented** | a task plus proposed decisions | `TermTriageResult` |
| `TermTriageAgent` (Protocol) / `UnconfiguredTermTriageAgent` | **Implemented (placeholder)** | `TermTriageTask` | raises `TermTriageNotConfiguredError` |
| `ClaudeTermTriageAgent` | **Implemented** | `TermTriageTask` | validated `TermTriageResult` + `TermTriageRuntimeSummary` |
| `validate_term_triage_result` | **Implemented** | a task + a proposed result | bounded, correctable/non-correctable issues |
| Persistence, migration, job type, worker handler, API endpoint, pipeline/scheduling wiring | **Not built** | — | — |

## Scope (settled, not to be silently widened)

The only question this subsystem answers per term: is it `document_metadata`,
a `potential_line_item`, or genuinely `uncertain`? It has **no evidence
tools of any kind** and makes **no applicability claim** — no scope, no
candidate IDs, no evidence citation. It never deletes or normalizes a term:
every term stays in the original `DocumentAnalysisResult` artifact regardless
of its triage disposition. `document_metadata` means "not automatically sent
to applicability attribution," never "discarded" or "impossible to revisit."

`uncertain` is always sent to `TermApplicabilityAgent`, on equal footing with
`potential_line_item`. Triage must never use keyword filtering as its sole
decision mechanism, and must treat any term touching service period,
billing/invoicing frequency, payment terms, currency, quantity, pricing,
discount, tax, renewal, commitment, or order dates conservatively rather than
assuming it is administrative.

## Selection rule (implemented in `term_applicability.contracts`)

`build_selected_term_applicability_task(full_task, triage_result)` selects
every `potential_line_item` and every `uncertain` term; `document_metadata`
terms are not automatically selected. `TermApplicabilityTask.terms` still
holds every original term (audit identity); `selected_term_ids` is the
explicit, trusted, tenant/run-safe subset `TermApplicabilityAgent` may
actually decide about. A decision naming a term outside `selected_term_ids`
is rejected by `validate_term_applicability_result`, distinct from a
reference to a term entirely outside the task. Omitted `document_metadata`
terms are preserved as un-attributed — not silently turned into `unknown`
applicability decisions, which would misrepresent "never sent for
attribution" as "attribution was attempted and inconclusive."

## Finalization design (mirrors the other agents)

Evidence-free finalizer (`finalize_term_triage`) accepting only compact
decisions; trusted identity/version fields injected server-side and rejected
if Claude includes them; exactly one correction permitted, only for
correctable completion-gate issues (unknown-term reference, incomplete
coverage); accepted result kept only in memory; no database or storage write
anywhere in this package.

## Runtime observability contract

`TermTriageRuntimeSummary` mirrors the sibling agents' runtime summaries
wherever the concept is shared (session init timing, SDK event categories,
finalizer attempts/acceptance, correction count, terminal reason, total
duration, token/cost fields only when actually returned) and omits
everything tied to evidence retrieval, since there are no read-only tools
here. It never logs prompts, hidden reasoning, term bodies beyond
diagnostic-safe identifiers/counts, paths, SQL, or credentials.

## Deferred

Persistence (`term_triage_runs` or equivalent), job type, worker handler,
API endpoint, scheduling ahead of `TermApplicabilityAgent` in the pipeline,
and a controlled live smoke test of the two-stage pipeline together.
