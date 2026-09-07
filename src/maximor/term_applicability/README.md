# Term-applicability subsystem

This is **Step 2 of 3** (matching the `document_analysis`/`sku_mapping` build
pattern): a functional, narrow-tool Claude reasoning layer exists and can be
invoked directly with a real task and real persisted evidence. No
persistence, migration, job type, worker handler, API endpoint, or
pipeline/scheduling wiring exists yet — nothing here is reachable from a live
upload, and no code in this package makes a paid Claude request except when a
caller explicitly constructs `ClaudeTermApplicabilityAgent` and calls
`execute`/`resolve` with a live API key.

## Why this is a separate subsystem

Adding term-to-candidate applicability reasoning directly inside
`DocumentAnalysisAgent` caused repeated live timeouts against real documents —
the added reasoning burden scaled with terms × candidates and pushed the
agent past its bounded timeout. `DocumentAnalysisAgent` now extracts only raw
`GlobalTerm`s and evidence; every term is persisted with
`applicability_scope="unknown"` and no candidate IDs, assigned server-side,
never by that agent. `TermApplicabilityAgent` resolves that question
afterward, as its own bounded stage — and it is deliberately not folded into
normalization either, because normalization must consume already-resolved
term/candidate relationships, not infer them itself.

## Components

| Component | Status | Input | Output |
|---|---|---|---|
| `TermApplicabilityTask` / `build_term_applicability_task` | **Implemented** | one already-validated `DocumentAnalysisResult` + `analysis_run_id` | `TermApplicabilityTask` |
| `TermApplicabilityRepository.load_task` | **Implemented** | tenant/document/preprocessing/analysis-run IDs | a trusted `TermApplicabilityTask`, or a typed source error |
| `TermApplicabilityDecision` / `TermApplicabilityResult` / `build_term_applicability_result` | **Implemented** | a task plus proposed decisions | `TermApplicabilityResult` |
| `TermApplicabilityToolset` (Protocol) / `PersistedTermApplicabilityTools` | **Implemented** | a fixed task + term/candidate/evidence IDs | one persisted evidence region, or a typed tool error |
| `validate_term_applicability_result` | **Implemented** | a task + a proposed result (+ optional runtime) | bounded, correctable/non-correctable issues |
| `TermApplicabilityAgent` (Protocol) / `UnconfiguredTermApplicabilityAgent` | **Implemented (placeholder)** | `TermApplicabilityTask` + toolset | raises `TermApplicabilityNotConfiguredError` |
| `ClaudeTermApplicabilityAgent` | **Implemented** | `TermApplicabilityTask` + toolset | validated `TermApplicabilityResult` + `TermApplicabilityRuntimeSummary` |
| `TermApplicabilityRuntimeSummary` | **Implemented** | — | — |
| Persistence, migration, `term_applicability` job type, worker handler, API endpoint, pipeline/scheduling wiring | **Not built (Step 3)** | — | — |

## Scope (settled, not to be silently widened)

The only question this subsystem answers: *for each extracted contract term,
is it commercially relevant to line items; if so, does it apply document-wide,
to specific product candidates, or remain unknown?*

It does not: select SKUs, parse dates or money, calculate totals, change a
candidate's commercial status, or invent missing values.

## Input shape (settled)

One batch per completed `document_analysis_run` — not one agent call per
term. `TermApplicabilityTask` carries identifier-only application context
(organization/document/preprocessing/analysis-run IDs, version labels) plus
compact `CandidateContext`/`TermContext` summaries: raw name/value,
commercial status, raw attributes, and **evidence IDs** (bare block/table
identifier strings), not full `EvidenceReference` objects. It never carries
full PDF text or the whole preprocessing JSON. Full evidence geometry
(bounding box, representation, extraction source) is resolved narrowly,
later, through `get_term_evidence_region`/`get_candidate_evidence_region` —
that is exactly why those tools exist and why the task doesn't pre-embed
that geometry for every term and candidate up front.

## Finalization design (implemented)

These safeguards, first documented in Step 1, are now implemented in
`ClaudeTermApplicabilityAgent` and must carry through unchanged into Step 3:

- The future LLM-facing finalizer submits **evidence IDs only**; server-side
  code resolves them to exact trusted `EvidenceReference` objects (matching
  `document_analysis`/`sku_mapping`'s own finalizer resolution pattern). The
  model is never required to reconstruct bounding boxes, representations, or
  extraction-source metadata.
- Application-owned identifiers and version fields (organization/document/
  preprocessing/analysis-run IDs, schema/prompt/skill/agent versions) are
  trusted and injected server-side; Claude cannot override them by including
  them in a submission, the same way `document_analysis`'s finalizer strips
  and re-injects `TRUSTED_IDENTITY_FIELDS`.
- The future MCP permission allowlist must use exact namespaced tool
  identifiers (e.g. `mcp__maximor_term_applicability__get_term_evidence_region`),
  never bare operation names.
- The future agent receives compact task context (this step's `TermApplicabilityTask`)
  and retrieves evidence narrowly; it must never load whole preprocessing
  artifacts and must never compare every term against every candidate — the
  skill's job is to make that comparison need rare, not exhaustive.
- `unknown` is a valid, **preferred** outcome whenever evidence cannot safely
  establish document-wide or candidate-specific applicability. It is not a
  failure state to be minimized by guessing.
- This agent does not select SKUs, normalize values, do arithmetic, or alter
  commercial status — those remain other subsystems' jobs.

## Runtime observability contract

`TermApplicabilityRuntimeSummary` (in `agent.py`) is a bounded dataclass,
structurally mirroring `DocumentAnalysisRuntimeSummary`/`SkuMappingRuntimeSummary`
wherever the concept is shared, with no SDK client behind it yet. It records:
session initialization start/end/duration; each local tool invocation's
start/end/duration/sequence/name/success; successful tool-call order and
counts; the last successful local tool return; timestamped SDK event
*categories/types only* (never content); finalizer submission/acceptance/
rejection counts; correction-attempt count; terminal kind/reason and total
elapsed duration; token/cost fields when an SDK reports them; and bounded
timing/event arrays with truncation flags. Durations use a monotonic clock;
correlation timestamps are UTC. It never claims visibility into hidden model
reasoning — `elapsed_after_last_successful_tool_return_ms` is labelled and
documented as an *observed interval* with no tool activity in it, not a
measurement of model "thinking time."

## Step 2 (implemented)

Narrow read-only tools (`get_term_evidence_region`, `get_candidate_evidence_region`)
bound to one fixed task, with `get_evidence_region` kept strictly as an internal
dependency of `PersistedTermApplicabilityTools` — never exposed to Claude as
its own tool; `ClaudeTermApplicabilityAgent` with a prompt, the guarded
finalizer described above, and one targeted in-session correction limited to
correctable validation errors; deterministic pre-acceptance validation
(`validate_term_applicability_result`), including a completion gate that
requires a successful evidence retrieval this run for every line-item
decision's evidence, and a successful candidate-evidence retrieval for every
named candidate in a `candidate`-scope decision.

## Step 3 (deferred, not started)

`term_applicability_runs` table + compressed canonical artifact + relational
projections, a `term_applicability` job type, `TermApplicabilityHandler`,
`DocumentAnalysisHandler` scheduling it (in parallel with eligible SKU-mapping
jobs, after analysis persistence and reload-validation succeed), and
normalization consuming both SKU-mapping and term-applicability results.

## Completeness coverage

The expanded result requires exactly one decision for every explicitly selected
term. Eligible `purchased`/`included` candidates expose raw-attribute hints
(`qty`, `unit_price`, and `total_contract_value`) as expected fields. Each such
candidate must have one `CandidateCommercialFactCoverage` record partitioning
every expected field into an evidence-backed extracted fact or an explicitly
unresolved field. Excluded and non-purchased candidates never receive coverage.
Raw values remain unchanged; this stage performs no normalization or arithmetic.
