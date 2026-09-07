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

This shows the current implemented endpoint first, then the planned normalization and finalization path.

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
  │     ├── inspect PDF, extract native text/layout/tables
  │     ├── render pages; OCR only when needed
  │     ├── persist/reload-validate PreprocessedDocument
  │     └── schedule document_analysis
  │
  ├── DocumentAnalysisHandler                            
  │     ├── Claude DocumentAnalysisAgent
  │     │     └── narrow persisted-document tools:
  │     │         overview, search, targeted page text/blocks/tables/renders,
  │     │         and evidence regions — never direct PDF or SQL access
  │     ├── persist/reload-validate canonical analysis
  │     └── fan out independently after success:
  │           │
  │           ├─────────────────────────────────────────────────────────┐
  │           ▼                                                         ▼
  │     sku_mapping jobs                                      term_applicability job
  │     (one per eligible candidate)                          (one per analysis run)
  │           │                                                         │
  │           ▼                                                         ▼
  │     SkuMappingHandler                                  TermApplicabilityHandler
  │     ├── load active tenant catalog                     ├── Claude TermTriageAgent
  │     ├── HybridSkuRetriever                             │     classifies raw terms as metadata,
  │     │     exact/alias → lexical → semantic seam        │     potential line-item, or uncertain
  │     ├── Claude SkuMappingAgent                         └── Claude TermApplicabilityAgent
  │     │     MATCH / NO_MATCH / AMBIGUOUS                       for selected terms only:
  │     └── persist/reload-validate decision                    scope + raw commercial facts
  │           │                                                   + evidence-backed coverage
  │           ▼                                                         │
  │     validated SKU mappings                                          ▼
  │                                                           validated commercial enrichment
  │
  └── Both completed branches become trusted inputs to normalization.

                         ┌───────────────────────────────────────────-┐
                         │ Deterministic normalization                │
                         │ ├── money/currency and price schedules     │
                         │ ├── dates and service periods              │
                         │ ├── quantities                             │
                         │ ├── payment terms and billing enums        │
                         │ └── explicit, evidence-backed derivations  │
                         └───────────────────────────────────────────┘
                                             │
                                             ▼
                         ┌───────────────────────────────────────────-┐
                         │ Final validation                           │
                         │ ├── schema, SKU, date, and decimal checks  │
                         │ ├── total/schedule reconciliation          │
                         │ ├── targeted semantic review only when     │
                         │ │   deterministic rules cannot decide      │
                         │ └── targeted correction to the owning      │
                         │     document, SKU, or term stage           │
                         └───────────────────────────────────────────┘
                                             │
                                             ▼
                         FinalOrderFormExtraction
                         ├── COMPLETED: every required check passes
                         └── REVIEW_REQUIRED / FAILED_VALIDATION:
                             safe diagnostics and no fabricated result
                                             │
                                             ▼
                              persisted result and tenant-scoped API
```

Important boundaries:

- PostgreSQL holds job state, tenant-scoped relational projections, and lineage. Object storage holds PDFs, page renders, and compressed canonical artifacts. Neither is passed wholesale through an agent prompt.
- `organization_id` scopes every record and tool call. `document_id` identifies the uploaded source; preprocessing, analysis, mapping, and enrichment runs are separate versioned records linked to it.
- The catalog repository and hybrid retriever are application services exposed to `SkuMappingAgent` through narrow tools. They are not inside Claude.
- `SKU_MAPPING` and `TERM_APPLICABILITY` are parallel after document analysis. Term enrichment does not wait for SKU mapping; normalization is the first future stage that consumes both results.


## Product Scale Decisions to consider:

Multi-tenancy
Every catalog lookup must be filtered by organization_id. One organization’s SKU must never appear in another organization’s results.


Asynchronous processing
Large PDFs should run as jobs:

Idempotency
Re-uploading the same request should not create duplicate extractions or duplicate API charges. Use document hashes and request idempotency keys.

Caching
Cache:
- PDF extraction results
- Page images
- OCR results
- SKU embeddings
- Unchanged catalog retrieval results
Do not blindly cache final answers across catalog versions.


Security
Order forms contain commercially sensitive information. The product should include:
- Encryption in transit and at rest
- Tenant-isolated storage
- Short-lived document access
- Audit logs
- Configurable retention and deletion
- Secrets stored outside source control
- No contract text in ordinary application logs


Observability
Record per job:
- Processing duration
- LLM calls
- Tokens and estimated cost
- Validation failures
- Correction attempts
- Retrieval candidates and scores
- Final confidence
- Model and prompt versions
- Catalog and schema versions
This is essential for improving the system safely.



| Component | Appropriate LLM use |
|---|---|
| Input validation | Detecting whether an uploaded document is actually an order form |
| PDF processing | Repairing corrupted OCR or interpreting visually complex tables |
| Document understanding | Primary LLM responsibility |
| Candidate detection | Primary LLM responsibility |
| Purchased-status classification | Primary LLM responsibility |
| SKU mapping | Primary LLM responsibility |
| Normalization | Ambiguous semantic values only |
| Validation | Semantic evidence and contradiction checks |
| Correction | Reinterpreting targeted fields |
| Ground-truth error analysis | Explaining prediction failures and proposing improvements |
| Human-review preparation | Producing concise explanations and highlighting relevant clauses |


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
