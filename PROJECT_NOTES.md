# Maximor AI Take-Home Notes

Use this file as a shared notebook for decisions, questions, experiments, and progress.

## Working Agreement

- Maintain architectural consistency across the discussion and implementation.
- Do not silently replace an earlier hybrid or agent-based decision with a deterministic-only design, or vice versa.
- When refining a decision, explicitly state the previous decision, the proposed change, and the reason for changing it before treating the new design as settled.
- Distinguish clearly between a simplified explanation and the complete architecture so simplification is not mistaken for a design change.
- Keep an explicit decision log for settled architectural choices.

## Project Goal

Build an agentic order-form parsing system that:

- Accepts an order-form PDF and a known SKU catalog.
- Extracts contract-level metadata and purchased contract items.
- Maps every extracted item to a valid catalog SKU.
- Produces structured JSON.
- Validates its output and measures accuracy against supplied ground truth.

## Data Overview

- 50 synthetic order-form PDFs, each two pages long.
- 50 preview-text files.
- 50 ground-truth JSON files.
- 21 catalog SKUs.
- 1 JSONL manifest connecting each PDF to its ground truth.
- 132 ground-truth contract items in total.

### SKU Catalog

Each SKU contains:

- `id`: Internal UUID.
- `sku_code`: Stable business identifier.
- `name`: Official product name.
- `description`: Product or service meaning.
- `parsing_instructions`: SKU-specific interpretation rules.
- `usage_count`: Undocumented metadata; it does not equal occurrences in these 50 forms.

### Ground Truth

Each `ground_truth/of-XXXX.json` file is the expected structured output for the matching `pdfs/of-XXXX.pdf` file. It is used to measure SKU identification and field-extraction accuracy.

The preview-text files are debugging aids, not ground truth and not intended as production parser inputs.

### Manifest

Each manifest record contains:

- `form_id`
- `quote_number`
- `pdf_path`
- `ground_truth_path`
- `contract_item_count`

The `/app/...` paths are stale for this machine and should be resolved relative to the local dataset directory. `contract_item_count` reveals part of the expected answer, so it should be used by the evaluation harness, not passed to the parsing agent.

## Current Architecture Direction

We havent finalised it yet.

## Learning and Evaluation

- The LLM does not automatically update its model weights from ground truth.
- The workflow can adapt by improving prompts, aliases, validation rules, examples, and error memory.
- Development examples may be used for that adaptation.
- A held-out test set must remain unseen until the workflow is frozen to avoid data leakage.


## Important Findings

- PDFs contain extractable text but include deliberate OCR-like substitutions such as `1` in place of `i`.
- The dataset ground-truth schema is narrower than the assignment description.
- Ground truth does not include fields such as `discount` or `unit_price_period`.
- The benchmark-compatible schema should match ground truth, while broader assignment fields can be optional extensions.
- Catalog `usage_count` values total 251, whereas the 50 forms contain 132 line items. Treat `usage_count` as untrusted auxiliary metadata.

## Open Questions

- Which Claude model and document-input method should be used?
- Should the primary parser use embedded PDF text, document vision, or a hybrid?
- What exact evaluation metrics and matching rules should define accuracy?
- How should repeated items with the same SKU be matched during evaluation?
- Should adaptive memory persist across runs or only generate proposed prompt/rule changes?

## Work Log

### Initial data review

- Confirmed that the local assignment PDF matches the pasted brief.
- Confirmed completeness of PDFs, preview text, ground truth, catalog, and manifest.
- Confirmed that the Notion page requires Maximor workspace authentication.
- Confirmed that the workspace has not yet been initialized as a Git repository.

## Personal Notes

Optimisation to keep note of:
Using different models could later be an optimization—for example, an expensive model for extraction and a cheaper model for validation—but it adds complexity that probably does not improve the initial submission.

Anthropic’s current documentation says tool-selection accuracy can degrade once more than approximately 30–50 tools are available simultaneously. It recommends tool search for large catalogs so only a few relevant definitions are loaded. Anthropic tool-search documentation

Should we copy Anthropic’s PDF skill?
We should use it as a design reference, but not copy it wholesale without checking its license. Its frontmatter identifies it as proprietary, and Anthropic describes its document skills as source-available rather than open source.
Also, it is intentionally general-purpose. It includes merging, splitting, rotation, watermarking, encryption and PDF creation—capabilities our parsing agent does not need.
so, i am using only parsing after reading the whole skills.md

One important limitation: do not run long PDF jobs using FastAPI’s simple in-process background tasks. FastAPI’s documentation recommends a separate worker system for heavy background computation. FastAPI background-task guidance


Agents with more deterministic rules work better, timestamp of video 1:09

How to handle context in agents so that we dont get diminishing returns. Say UX design, see video time stamp 1:17:00

The runner and dispatcher are ordinary Python components inside the same Python worker. Neither is an agent or separate server. The runner controls the lifecycle of background jobs, The dispatcher answers one simple question:
Which handler should execute this type of job? The dispatcher does not do the work, the Handler performs the actual job-specific work.

For your current implementation, making every internal tool an MCP server immediately would add:
- Another server process
- Transport configuration
- Tool serialization
- Authentication and authorization
- Deployment complexity
- More integration testing
Your narrow tools currently live in the same Python backend. Direct Python tool adapters are simpler and safer for the take-home. You can expose them through MCP later without redesigning the underlying services.

Do not introduce MCP yet.
Your internal structure should still allow MCP later:


## scalable end to end arch for now:

This reflects the current implemented pipeline, including normalization persistence and the final API
boundary. Automatic upstream correction remains future work.

```text
Client
  │ POST PDF upload
  ▼
FastAPI
  ├── validates the request and tenant
  ├── stores the source PDF under a relative object-storage key
  └── creates a document_preprocessing processing_job in PostgreSQL
  ▼
PostgreSQL processing_jobs queue
  │  (durable queue; a generic WorkerRunner claims jobs with SKIP LOCKED)
  ▼
Generic worker process
  WorkerRunner → JobDispatcher → job-specific handler
  │
  ├── DocumentPreprocessingHandler
  │     input: trusted organization/document/job identity + stored source PDF
  │     work:  native text, layout-block, and table extraction; page rendering;
  │            OCR only when needed
  │     output: persisted/reload-validated PreprocessedDocument with page inventory,
  │             representations, renders, and evidence locators
  │     └── schedules document_analysis
  │
  ├── DocumentAnalysisHandler
  │     input: trusted identity + completed PreprocessedDocument
  │     work:  Claude DocumentAnalysisAgent uses narrow persisted-document tools:
  │            overview, search, targeted page text/blocks/tables/renders, and
  │            evidence regions — never direct PDF, filesystem, or SQL access
  │     output: persisted/reload-validated DocumentAnalysisResult containing
  │             contract/pricing structure, ProductCandidates with raw attributes,
  │             CommercialStatus assessments, raw GlobalTerms, and evidence references
  │     └── schedules two independent downstream branches after persistence
  │           │
  │           ├─────────────────────────────────────────────────────────┐
  │           ▼                                                         ▼
  │     SKU mapping jobs                                       term_applicability job
  │     (one per eligible candidate)                           (one per analysis run)
  │           │                                                         │
  │           ▼                                                         ▼
  │     SkuMappingHandler                                    TermApplicabilityHandler
  │     input: candidate + commercial-status evidence +      input: analysis candidates, raw GlobalTerms, and evidence
  │            active tenant catalog                          │
  │     work: deterministic shortlist retrieval, then         ├── Stage 1: TermTriageAgent
  │           Claude judgment over retrieved candidates        │     input: all raw GlobalTerms
  │     output: MATCH / NO_MATCH / AMBIGUOUS decision,         │     output: one decision per term:
  │             cited evidence, catalog version, persisted run │             document_metadata / potential_line_item / uncertain
  │                                                          │     handoff: selects only potential_line_item + uncertain terms
  │                                                          │
  │                                                          └── Stage 2: TermApplicabilityAgent + commercial enrichment
  │                                                                input: selected terms + candidate context + evidence
  │                                                                work: retrieves supporting term/candidate evidence;
  │                                                                      resolves document / candidate / unknown scope;
  │                                                                      extracts raw candidate commercial facts
  │                                                                output: applicability decisions, candidate fact bundles,
  │                                                                        coverage declarations, evidence references
  │           │                                                         │
  │           └────────────────────────┬────────────────────────────────┘
  │                                    ▼
  └── NormalizationHandler (idempotent fan-in after both branches are terminal)
        input: completed analysis + current mapping runs + term-enrichment run
        work:  deterministic money/date/quantity/enum normalization, conservative
               term inheritance, Decimal reconciliation, and bounded semantic review
               only where deterministic rules cannot resolve semantic ambiguity
        output: persisted FinalOrderFormExtraction with normalized line items, field
                provenance, review issues, semantic findings, and business status:
                COMPLETED / REVIEW_REQUIRED / FAILED_VALIDATION
                                    │
                                    ▼
                     tenant-scoped API and dashboard result
```

Important boundaries:

- PostgreSQL holds job state, tenant-scoped relational projections, and lineage. Object storage holds PDFs, page renders, and compressed canonical artifacts. Neither is passed wholesale through an agent prompt.
- `organization_id` scopes every record and tool call. `document_id` identifies the uploaded source; preprocessing, analysis, mapping, and enrichment runs are separate versioned records linked to it.
- The catalog repository and hybrid retriever are application services exposed to `SkuMappingAgent` through narrow tools. They are not inside Claude.
- `SKU_MAPPING` and `TERM_APPLICABILITY` are parallel after document analysis. Term enrichment does not wait for SKU mapping; normalization is the first implemented stage that consumes both results.


## Product Scale Decisions: current baseline and next hardening

### Multi-tenancy — baseline implemented

Every document, run, job, catalog lookup, artifact, API read, and agent tool call is scoped by
`organization_id`. Composite relationships and tenant-scoped reads prevent a candidate or SKU from one
organization being used by another. In a production deployment, the API must derive this identifier from
authenticated identity rather than trust a client-supplied path value.

### Asynchronous processing — implemented

PostgreSQL `processing_jobs` is the durable queue. Generic workers atomically claim queued work using
`FOR UPDATE SKIP LOCKED`; `WorkerRunner` owns generic job states, and handlers own their domain run rows.
This supports multiple worker processes, retries after terminal failures, and parallel fan-out without
requiring Redis or Celery at the current scale. A dedicated broker or workflow engine becomes worthwhile
when throughput, cross-region delivery, or long-running orchestration needs exceed PostgreSQL's limits.

### Idempotency and lineage — partly implemented, needs API hardening

Completed artifacts and domain runs are immutable and versioned. Active downstream jobs are protected by
partial unique indexes, so the same active analysis/candidate/enrichment work is not run concurrently.
Every accepted result can be traced through document, preprocessing, analysis, mapping, and enrichment
run IDs. The upload API should next add a client idempotency key plus content-hash policy, so repeated
requests cannot create duplicate uploads or paid calls.

### Caching — deliberate future work

The system already persists authoritative preprocessing artifacts, renders, and canonical accepted
agent results. Future cache candidates are:

- deterministic preprocessing by source checksum and preprocessing version;
- rendered pages and OCR by page/render configuration;
- catalog embeddings and unchanged catalog retrieval results by catalog version;
- safe, bounded retrieval fragments keyed by the source run and tool version.

Never reuse a final document analysis, SKU mapping, enrichment, or normalized extraction across a changed
document, prompt/schema version, or catalog version without explicit compatibility rules.

### Security and privacy — baseline plus production requirements

Current safeguards include relative storage keys only, tenant-scoped reads, narrow tools, no arbitrary
filesystem/SQL tools for agents, secrets outside source control, and bounded diagnostics that exclude
document bodies, prompts, tool payloads, paths, and credentials. Production additionally requires:

- encryption in transit and at rest;
- authenticated tenant resolution and authorization at every API boundary;
- short-lived storage access, retention/deletion controls, and audit access logs;
- key rotation, secret management, and least-privilege database/storage roles;
- a documented data-processing policy for external LLM calls.

### Observability — implemented for agent stages; continue standardizing

Each job/run should retain safe, bounded metadata for:

- status transitions and processing duration;
- agent session initialization, tool-call timing, terminal reason, and correction count;
- validation failures, retrieval candidate scores, model/prompt/skill/schema/catalog versions;
- SDK token/cache-token/cost values when the provider returns them; unavailable values remain `null`, not
  fabricated;
- the lineage IDs required to reproduce or audit a result.

This information must remain content-free unless a separately authorized audit view is used.



Claude SDK session setup and teardown
An in-process MCP tool server with only narrow document-reading tools exposed


A guarded finalize_document_analysis tool as the only valid completion path: Clude cannot merely send ordinary chat text and have it treated as the result. It must call finalize_document_analysis(...) with structured output. Your server then checks it before accepting it.

Server-side identity and evidence validation: 
 Model does not get to choose or invent trusted identifiers:
- The server already knows the organization, document, preprocessing run, and analysis run.
- It injects those trusted IDs into the final result.
- The model can cite only evidence IDs that belong to that document/run.
- The server checks that cited candidate IDs and evidence references really exist and belong together

Bounded correction handling:
If the finalizer rejects a structurally close result—for example, an invalid evidence ID—the agent receives a compact correction message and gets a limited chance, such as one correction, to resubmit. It is not allowed to loop indefinitely, spend unlimited money, or silently accept an invalid result.

Timeout and cancellation handling



### Where LLMs belong

| Component | Primary responsibility | Appropriate LLM role |
|---|---|---|
| Upload and file admission | Deterministic validation, size/type limits, tenant authorization | Optional future classification of an unclear document type; never an authorization decision |
| PDF preprocessing | Deterministic inspection, text/layout/table extraction, rendering, OCR fallback | None in the current pipeline; do not use an LLM to silently repair OCR or PDF structure |
| Document analysis | Narrow persisted-document retrieval and evidence validation | Primary interpretation: contract structure, candidates, commercial status, raw terms, and evidence-backed findings |
| Term triage and commercial enrichment | Deterministic scope/coverage gates and evidence retrieval | Classify terms; resolve supported scope; extract raw candidate commercial facts from retrieved evidence |
| SKU retrieval | Application-owned tenant/catalog-scoped retrieval | None: exact/alias and lexical retrieval are deterministic; semantic retrieval remains a controlled future service seam |
| SKU mapping decision | Catalog membership and evidence validation | Compare retrieved candidates with document evidence; return MATCH, NO_MATCH, or AMBIGUOUS |
| Normalization | Decimal parsing, date/quantity parsing, enum mapping, explicit derivations | Only resolve a narrowly defined semantic ambiguity; it must not override deterministic arithmetic or invent values |
| Final validation | Schema, evidence, SKU integrity, date consistency, and total reconciliation | Optional targeted semantic review of contradictions after deterministic checks identify an ambiguity; never waive a deterministic failure |
| Targeted correction | Route and apply a bounded correction to the owning stage | Reinterpret only the cited problematic field/evidence; do not rerun the whole pipeline by default |
| Evaluation and human review | Deterministic comparisons, audit artifacts, and review queue | Explain discrepancies or summarize cited clauses, clearly separated from the authoritative extraction |


## Fixed Architecture Principle: Narrow Typed Tools

Agents must not receive unrestricted Bash, arbitrary SQL, unrestricted filesystem access, or a generic database-write capability. Each agent receives only narrowly defined, typed tools required for its role.

Initial tool contracts:

- `search_document(...)`
- `retrieve_skus(...)`
- `get_authoritative_sku(...)`
- `normalize_money(...)`
- `normalize_date(...)`
- `calculate_line_total(...)`
- `reconcile_document_total(...)`
- `validate_extraction(...)`
- `finalize_extraction(...)`

The hybrid SKU retrieval subsystem must follow the same principle. Exact, lexical/trigram, and pgvector searches are controlled retrieval operations scoped by `organization_id` and `catalog_version_id`. The `SkuMappingAgent` should normally receive a single `retrieve_skus(...)` tool plus authoritative lookup, rather than arbitrary database or SQL access.

The LLM proposes interpretations. Typed deterministic tools perform retrieval, transformations, arithmetic, validation, and persistence.

### Narrow Document Tools Reduce Token Usage

The complete lossless preprocessing result remains in object storage and PostgreSQL for
traceability, but `DocumentAnalysisAgent` must not receive the entire stored document
representation on every Claude call. It should begin with a compact document and page
inventory, then use narrow typed tools to retrieve only the relevant page text, layout
blocks, tables, OCR text, evidence regions, or page renders.

Compression reduces storage and transfer size only. It does not reduce model tokens after
the content is decompressed. Selective retrieval through narrow tools is what controls LLM
context size, repeated input-token cost, and irrelevant-document noise while preserving all
representations in authoritative storage.

### Guarded Finalization

`finalize_extraction(...)` is the only tool allowed to persist an accepted final result. It must refuse to save a result with status `COMPLETED` unless all required deterministic checks pass:

- Output conforms to the versioned JSON schema.
- Every selected SKU exists in the authoritative catalog.
- SKU ID, code, and name agree.
- SKU belongs to the correct organization and catalog version.
- Required evidence references point to stored document pages or blocks.
- Required dates are valid and temporally consistent.
- Monetary values use decimal-safe normalization.
- Required arithmetic and reconciliation checks pass within an explicit tolerance.
- Enum values are allowed.
- No unresolved ambiguity remains for a required field.

Failed attempts may still be stored for audit and debugging, but only with a non-accepted status such as `FAILED_VALIDATION` or `REVIEW_REQUIRED`.

This is a settled design decision. Any future proposal to broaden tool permissions or bypass guarded finalization must be called out explicitly before implementation.

## Decision Log

### 2026-09-06 — SKU-mapping persistence: full audit-grade model

The SKU-mapping subsystem (`SkuMappingTask`, `SkuRepository`/`PostgresSkuRepository`,
`HybridSkuRetriever`/`DeterministicHybridSkuRetriever`, `SkuMappingDecision`,
`SkuMappingToolset`, `validate_sku_mapping_decision`, the `sku-mapping` skill,
`ClaudeSkuMappingAgent`) is implemented and has been verified end to end with real
paid Claude API calls against a real order form, correctly mapping all candidates
including an abbreviated-text case and an ambiguous-commercial-status case. It
currently has no persistence at all: a `SkuMappingDecision` exists only in memory
for one process invocation and is discarded on exit.

Settled decision: give it the **same full audit-grade persistence model** as
`document_analysis`, not a single lightweight decisions table. A SKU decision is a
high-value audit record — it determines what the system reports as purchased and
which catalog item it maps to, and normalization will build on it later — so how it
was reached must be preserved, not just its final answer.

- **`sku_mapping_runs`** — one row per candidate mapping attempt, mirroring
  `document_analysis_runs`: organization/document/analysis-run identifiers, a
  `document_product_candidate_id` foreign key (the internal UUID row in
  `document_product_candidates` — **not** the external string candidate id such as
  `pc_talent_acquisition_core`, which stays inside the canonical artifact/projection
  as reference data only), a `processing_job_id` for when worker wiring is added,
  attempt number and status, model/prompt/skill/agent/schema/retriever version
  fields, `catalog_version_id`, token/cost/runtime diagnostics, and a validation
  status with a safe failure reason.
- **Accepted artifact**: one compressed canonical JSON artifact in object storage
  per accepted run, holding the mapping task identity, the retrieved SKU shortlist
  with scores/sources, the accepted `SkuMappingDecision`, cited evidence ids, and
  catalog version identity.
- **Relational projections** for accepted decisions only: outcome
  (`match`/`no_match`/`ambiguous`), selected internal SKU UUID/code/name when
  matched, rationale, considered SKU ids when ambiguous, evidence references.
- **Failed/rejected attempts** are persisted too, but only as run metadata and safe
  diagnostics — never as an accepted artifact. Same discipline
  `document_analysis_runs` already enforces (a non-completed run has no canonical
  artifact).
- **`catalog_version_id` is always stored** — a decision is only reproducible if the
  exact catalog snapshot Claude and retrieval used is known.
- **Immutability with derived supersession, not mutation.** Catalog changes never
  overwrite a historical decision. A new catalog snapshot produces a *new*
  `sku_mapping_runs` row carrying `supersedes_mapping_run_id → old_run.id` (an
  explicit forward lineage link). The old accepted run's `status` stays
  `completed` and its result remains historically true as-is; "superseded" is a
  *derived* fact (whether any later run points back at it), never a field mutated
  on the old row. The persistence service must enforce that a replacement run
  shares the same organization, document, analysis run, and candidate as the run
  it supersedes.

Auditable chain this produces: `PDF → preprocessing run → document analysis run →
candidate → SKU mapping run → catalog version → accepted decision`, with lineage
across catalog versions traceable via `supersedes_mapping_run_id`.

Not yet decided (separate, later conversations): linking document-wide `GlobalTerm`
facts (dates, payment terms, invoicing frequency) to the correct SKU (a
`document_analysis`-side gap, confirmed against real data — `GlobalTerm` has no
candidate-linking field at all); and the worker/job wiring shape connecting
`SkuMappingAgent` to candidates as `DocumentAnalysisAgent` produces them.

### 2026-09-06 — SKU-mapping worker wiring: automatic scheduling and job execution

The persistence model above is now load-bearing: `SkuMappingHandler` runs as a
real `processing_jobs` job type, and `DocumentAnalysisHandler` schedules that job
automatically for every eligible candidate once its own analysis run is
persisted and reload-validated.

- **`JobType.SKU_MAPPING`** added. `processing_jobs` gained two nullable columns,
  `analysis_run_id` and `document_product_candidate_id`, populated for
  `sku_mapping` jobs only. A `CHECK` constraint (`sku_mapping_link_required`)
  enforces both fields are set together exactly when `job_type = 'sku_mapping'`
  and null otherwise — a job's type alone determines whether it carries a SKU
  target.
- **Tenant-safe composite foreign keys**, not per-column FKs: `(organization_id,
  document_id, analysis_run_id)` → `document_analysis_runs`, and
  `(organization_id, analysis_run_id, document_product_candidate_id)` →
  `document_product_candidates`. This makes it impossible at the database level
  for a job to reference an analysis run belonging to another organization/
  document, or a candidate belonging to another analysis run — verified by a
  dedicated cross-lineage rejection test, not just by application code.
  Supporting composite-unique constraints were added to
  `document_analysis_runs` and `document_product_candidates` to make these FKs
  possible.
- **Partial unique index** on `(analysis_run_id, document_product_candidate_id)`
  scoped to `job_type = 'sku_mapping'` and non-terminal status (`queued`,
  `claimed`, `running`) prevents two concurrent SKU-mapping jobs for the same
  candidate, while still allowing a fresh sequential attempt once a prior job
  reaches a terminal status — the same idempotent-scheduling shape
  `schedule_document_analysis_job` already uses.
- **`SkuMappingEligibilityPolicy` (versioned, deterministic)**: `purchased` and
  `included` candidates schedule a mapping job; `optional`, `excluded`, and
  `mentioned` are skipped outright; `ambiguous` is a `review` disposition and is
  explicitly **not** auto-scheduled — an ambiguous commercial status should not
  silently trigger a SKU lookup that presumes the item was ordered.
- **Scheduling reads persisted projections, not the in-memory result.**
  `DocumentAnalysisHandler._schedule_eligible_sku_mapping_jobs` runs only after
  the existing persist-then-reload-validate step succeeds, and queries the
  `document_product_candidates`/`document_commercial_status_assessments` rows
  it just wrote — those rows are the only place the internal
  `document_product_candidate_id` a job needs actually exists.
- **`SkuMappingHandler` treats `match`, `no_match`, and `ambiguous` as completed
  jobs**, not failures. All three are business outcomes the agent is explicitly
  designed to reach; only a genuine technical failure (source/candidate
  unavailable, retrieval missing, persistence/reload failure, an unhandled
  exception) raises `JobExecutionError`. This mirrors `DocumentAnalysisHandler`:
  the generic worker runner remains the sole owner of `processing_jobs`
  terminal status, and a handler's own domain-run status (`sku_mapping_runs`)
  is tracked separately.

Deferred, unchanged from the prior entry: linking `GlobalTerm` facts to SKUs.

### 2026-09-06 — Global-term applicability

Term applicability is owned by `DocumentAnalysisAgent`; raw term values remain
unnormalized, and unknown applicability is preserved for later review.

### 2026-09-06 — Global-term applicability decoupled from DocumentAnalysisAgent (supersedes the entry above)

Previous decision (above, same day): `DocumentAnalysisAgent` itself classified term
applicability. A live timeout investigation against `of-0006` found this reasoning step
measurably increased model output/reasoning time, and a prompt-only attempt to make it
more conservative did not resolve a second live timeout. Settled change:
`DocumentAnalysisAgent` extracts raw terms and evidence only (`term_id`, `raw_name`,
`raw_value`, evidence) — it no longer reasons about, receives, or emits applicability.
Applicability resolution is deferred to a separate, not-yet-built post-analysis
subsystem (`TermApplicabilityAgent`); `unknown`/`()`, assigned server-side, is the
correct persisted initial state for every term until that subsystem exists.

`ApplicabilityScope`, `GlobalTerm`'s fields, tables, migrations, and persistence
projections are unchanged. A new compact `DraftGlobalTerm`/`DraftDocumentAnalysisResult`
pair (`maximor/document_analysis/draft.py`) is the only shape the finalizer now accepts;
`promote_draft_result` is the sole, deterministic conversion to the canonical
`DocumentAnalysisResult`.

### 2026-09-06 — Normalization Stage 1: final-output contracts and trusted-input assembly only

`maximor/normalization/` now exists as **Stage 1 of 5**: versioned final-output
contracts (`FinalOrderFormExtraction`, `NormalizedOrderMetadata`, `NormalizedLineItem`,
`MoneyValue`, `PriceScheduleEntry`, `FieldProvenance`) and deterministic trusted-input
assembly (`NormalizationRequest`, `NormalizationInput`, `LineItemSourceBundle`,
`assemble_normalization_input`, `assemble_line_item_source_bundles`,
`NormalizationRepository`). No money/date/quantity normalization, arithmetic,
correction loop, job type, worker handler, API endpoint, or Claude call exists yet —
nothing here is reachable from a live upload.

- **Contracts only for the final shape.** `NormalizedOrderMetadata`/`NormalizedLineItem`
  fields are optional everywhere a document may honestly omit a value (no invented
  `quantity=1` default). Each carries one `field_provenance: dict[str, FieldProvenance]`
  keyed by its own field names, rather than a parallel `*_provenance` field per value.
- **`FieldProvenance`** records `ProvenanceSourceType` (`candidate_fact`, `document_term`,
  `sku_mapping`, `derived`) and `ValueOrigin` (`extracted`, `inherited`, `derived`), with a
  validator requiring exactly the identity field(s) each source type implies.
- **`NormalizationInput` bundles one already-completed `DocumentAnalysisResult`, one
  completed combined term-applicability artifact (triage + applicability + facts +
  coverage), and one completed, current (non-superseded) `SkuMappingRunArtifact` per
  `purchased`/`included` candidate** (`sku_mappings`/`sku_mapping_run_ids`, keyed by
  external candidate id). Its own `model_validator` — not just `assemble_normalization_input`
  — independently re-enforces that every eligible candidate has an entry, so a directly
  constructed bad instance is rejected the same way.
- **Every eligible candidate must resolve to a completed SKU mapping (any outcome) and,
  if it has expected raw-fact hints, a completed `CandidateCommercialFactCoverage`** —
  either missing surfaces a typed `NormalizationSkuMappingMissingError`/
  `NormalizationCommercialFactCoverageMissingError` from `assemble_normalization_input`;
  an eligible candidate can never silently disappear.
- **Only a `MATCH` SKU-mapping outcome produces a `LineItemSourceBundle`.** A `NO_MATCH`/
  `AMBIGUOUS` mapping is not an assembly error — it is a complete, honest answer already
  captured in `NormalizationInput.sku_mappings` — it simply yields no bundle.
  `excluded`/`optional`/`mentioned`/`ambiguous` candidates are never eligible at all.
- **Document-wide terms are not inherited onto line items in this stage.**
  `LineItemSourceBundle.available_document_terms`/`available_candidate_terms` surface
  every relevant applicability decision without merging it into the bundle's own facts —
  inheriting a document-wide term onto a specific line item is explicitly a later
  normalization stage's decision.
- **`NormalizationRepository`** resolves each eligible candidate's *current* (non-superseded)
  completed `sku_mapping_runs` row with one narrow, tenant-scoped query (mirroring the
  derived-supersession rule already settled for SKU-mapping runs), then reuses the existing
  `DocumentAnalysisPersistenceService`/`TermApplicabilityPersistenceService`/
  `SkuMappingPersistenceService.load_completed_result` for all canonical content — no new
  raw-SQL artifact reads.

Not yet decided (Stage 2+): how a deterministic normalizer actually parses
money/dates/quantities, how document-wide term inheritance onto line items is decided,
the correction-loop shape, and the `normalization` job type/worker/API wiring.

### 2026-09-07 — Normalization Stages 2–3

- Primitive normalization and provisional line-item assembly are implemented
  together so parsing decisions can immediately retain field-level provenance
  and expose unresolved values without mutating trusted Stage 1 inputs.
- No invented defaults are permitted: quantities, currencies, dates, and totals
  remain absent when raw evidence is missing, ambiguous, or unsupported.
- Explicit Decimal derivations are limited to unambiguous quantity × unit-price,
  total ÷ quantity, and complete compatible schedules; contradictions are surfaced
  as review issues rather than resolved silently.
- Document-term inheritance, full-order reconciliation, LLM ambiguity assistance,
  final validation/correction, and pipeline wiring remain deferred to Stages 4–5.

### 2026-09-07 — Normalization Stage 4

- Document-term inheritance is conservative and field-specific: direct candidate
  facts win, candidate-scoped supported terms come next, and exact supported
  document-scoped aliases are last. Unknown and metadata terms are never inherited.
- The alias registry is intentionally small and exact after case/whitespace
  normalization; no fuzzy wording or semantic inference is permitted.
- Deterministic validation/reconciliation records date, currency, schedule,
  quantity, and total conflicts as bounded review issues rather than resolving
  them. Semantic review, correction, completion semantics, and pipeline wiring
  remain deferred to Stage 5.

### 2026-09-07 — Normalization Stage 5A: deterministic finalization readiness

- Stage 5A adds a pure finalization boundary with three explicit outcomes:
  `READY_FOR_SEMANTIC_REVIEW`, `REVIEW_REQUIRED`, and `FAILED_VALIDATION`.
  A clean deterministic result is intentionally not `COMPLETED`; semantic
  review and any bounded correction must happen before completion is possible.
- The gate defensively re-runs Stage 4 checks, verifies trusted lineage,
  MATCH SKU identity, candidate eligibility, and field-level evidence
  provenance, and classifies missing/ambiguous/conflicting observations for a
  future review queue without repairing or inventing values.
- Stage 5A remains pure and local. Semantic review, targeted correction,
  persistence/job/API wiring, final completion semantics, and end-to-end
  evaluation remain deferred.

### 2026-09-07 — Normalization Stage 5B: bounded semantic review

- Deterministic validation owns arithmetic, lineage, provenance, SKU identity,
  and integrity. Only explicitly queued `AMBIGUOUS_VALUE`,
  `SEMANTIC_SCOPE_REQUIRED`, and selected evidence-supported
  `CONFLICTING_VALUES` items may reach semantic review.
- The semantic reviewer is request-bound and read-only. It may return an
  evidence-backed interpretation, insufficient evidence, an unresolved
  conflict, or a precise correction recommendation, but it cannot mutate
  normalized values or apply a correction.
- Persistence, correction execution, final `COMPLETED` semantics, pipeline
  wiring, and paid smoke testing remain deferred until these local contracts
  are proven.

### 2026-09-07 — Normalization Stage 5C: persistence and dependency fan-in

- Normalization now has a tenant-scoped domain run and canonical compressed
  artifact. Relational projections expose normalized line items, bounded
  finalization issues, and semantic-review findings without replacing the
  canonical snapshot.
- A single idempotent fan-in service schedules normalization only after the
  SKU-mapping and term-applicability branches are terminal. The generic
  worker runner remains the sole owner of processing-job status transitions;
  normalization owns only its domain status.
- `completed`, `review_required`, and `failed_validation` are business
  terminal outcomes. Automatic correction execution, final reconciliation,
  and broader evaluation remain deferred.
