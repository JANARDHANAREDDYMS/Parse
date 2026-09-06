# Claude Notes

Running log of work done by Claude Code on this project, updated at the end of
every run with concrete bullet points. Companion to `PROJECT_NOTES.md` (the
human decision log) — this file tracks what Claude actually did, not what was
decided; decisions still belong in `PROJECT_NOTES.md`.

## Naming conventions observed so far (carry forward into new code)

- Per-domain package layout: `maximor/<domain>/{contracts.py, schemas.py,
  errors.py, versions.py, persistence.py, agent.py, repository.py, tools.py,
  tool_schemas.py, validation.py}`.
- Pydantic base models set `model_config = ConfigDict(extra="forbid",
  frozen=True)` (see `AnalysisModel` in `document_analysis/contracts.py`).
- External/business identifiers use the shared `Identifier` pattern:
  `Annotated[str, Field(min_length=1, max_length=128,
  pattern=r"^[A-Za-z0-9._:-]+$")]`.
- Version constants live in one `versions.py` per domain, named
  `<DOMAIN>_SCHEMA_VERSION`, `<DOMAIN>_AGENT_VERSION`, `<DOMAIN>_PROMPT_VERSION`,
  `<SKILL_NAME>_SKILL_VERSION`. Never derived from dates or env.
- Errors: one base `<Domain>Error(Exception)` carrying `code` + `safe_message`
  (no paths/content/tracebacks), with narrow subclasses like `XNotFoundError`,
  `XMismatchError`.
- Request contracts are named `<Domain>Request`: identifier-only, frozen,
  `extra="forbid"`, never carry document/content payloads.
- Toolsets are `Protocol` classes named `<Domain>Toolset`.
- DB models use `UUIDPrimaryKeyMixin` + `TimestampMixin` + `Base`; tenant
  isolation via `organization_id` FK (`ondelete="CASCADE"`); cross-tenant
  integrity via composite `ForeignKeyConstraint` on
  `(organization_id, <parent>_id)` tuples (see `Sku` -> `catalog_versions`).
- Migrations: `000N_<description>.py`, sequential, `revision` string mirrors
  the filename.

### Naming collision — resolved

`maximor/catalog/repository.py` already defines `SkuRepository` — the
**admin/write-side** catalog loader (upserts JSON catalog into Postgres via
the `catalog/loader.py` CLI). `PROJECT_NOTES.md`'s architecture also calls the
**read/retrieval-side** interface used by the SKU-mapping subsystem
`SkuRepository`. Decision: keep `maximor.catalog.repository.SkuRepository`
untouched; the new read-only retrieval interface lives in a new
`maximor/sku_mapping/` package and is named `SkuCatalogRepository`.

### Worker/runner/dispatcher infra (confirmed against code, not just notes)

- `ProcessingJob` rows are the unit of work; `job_type` is constrained by both
  a DB check constraint and the `JobType` StrEnum
  (`maximor/jobs/types.py`) — currently exactly 3 values: `pipeline_smoke_test`,
  `document_preprocessing`, `document_analysis`. No SKU-mapping job type exists.
- `WorkerRunner.run()` (`worker/runner.py`) polls; each tick calls
  `claim_next_job`, which does `SELECT ... FOR UPDATE SKIP LOCKED` scoped to
  `dispatcher.registered_job_types`, ordered by `created_at, id` — safe for
  concurrent worker processes.
- The claim becomes an immutable `JobContext` (org/document/job ids only, no
  DB session) passed to `JobDispatcher.dispatch`, which is a pure
  `JobType(context.job_type) -> handler` lookup into a
  `Mapping[JobType, JobHandler]` built once in `build_worker()`
  (`worker/__init__.py`).
- Every handler satisfies one Protocol: `async def execute(context) -> None`
  and must raise on failure. Terminal `processing_jobs.status` is **only**
  ever set by `WorkerRunner.process_one()` (`mark_job_completed` /
  `mark_job_failed`) — a handler may own its own domain-level run row (e.g.
  `document_analysis_runs`) but never the generic job's status.
- Stage chaining is a handler side effect on success:
  `DocumentPreprocessingHandler` calls `schedule_document_analysis_job(...)`
  (`jobs/service.py`), an idempotent create-or-return guarded by a **partial
  unique index** `uq_processing_jobs_analysis_preprocessing_run` (scoped to
  `job_type='document_analysis'` + active statuses), added together with a
  `processing_jobs.preprocessing_run_id` FK in migration
  `0006_analysis_job_link.py`.
- **Nothing SKU-mapping-related exists yet** in `JobType`,
  `worker/handlers/__init__.py`, or `build_worker()`'s dispatcher map, and
  there is no `0009`-style migration linking `processing_jobs` to a
  `document_analysis_runs.id`. A future `SkuMappingHandler(JobHandler)` would
  mirror `DocumentAnalysisHandler` exactly (build a `SkuMappingTask` from a
  loaded `DocumentAnalysisResult`, call the retriever/agent, persist its own
  run row) and would need its own `schedule_sku_mapping_job(...)` +
  linking migration. None of that is in scope for the current foundation work
  — confirmed explicitly with the user before touching `JobType`, the
  dispatcher, or any migration.

## Run Log

### 2026-09-05 — Run 1: Context gathering, no code written

- Read `PROJECT_NOTES.md`, `README.md`, `docs/Take-Home-assignment.pdf`,
  `.claude/skills/order-form-analysis/SKILL.md`.
- Read `maximor/catalog/{schemas,repository,loader,__init__}.py`.
- Read DB models: `sku.py`, `catalog_version.py`, `db/models/__init__.py`,
  `document_analysis.py`; and migrations `0001` (foundation, incl. pgvector
  extension) through `0008` (analysis diagnostics).
- Read `maximor/document_analysis/{schemas,contracts,validation,versions,
  persistence,errors,export,__init__}.py`, the worker handler
  `worker/handlers/document_analysis.py`, and job scheduling in
  `jobs/service.py` / `jobs/types.py`.
- Confirmed in the running Postgres container: `vector` extension is
  installed/active; `pg_trgm` is available but not yet created; `skus` table
  currently holds the real 21-SKU catalog for one organization/catalog
  version already loaded via `catalog/loader.py`.
- Inspected a real persisted `DocumentAnalysisResult` sample
  (`tests/test_results/document_analysis/of-0001/attempt-9-result.json`) to
  confirm the actual shape of `product_candidates` / `commercial_statuses`
  coming out of the document-analysis agent.
- Identified the exact handoff point into the SKU-mapping subsystem per
  `PROJECT_NOTES.md`'s architecture diagram: eligible `ProductCandidate`s
  (with their evidence and linked commercial status) from a completed
  `DocumentAnalysisResult`. Eligibility rules (which commercial statuses
  qualify) are not yet pinned down anywhere — flagged as an open decision.
- Flagged the `SkuRepository` naming collision above before writing any code.
- Was asked to pause before implementing; created this notes file per
  instruction. No source files were changed this run.

### 2026-09-05 — Run 2: Naming decision + worker infra verification, no code written

- Asked the user to resolve the `SkuRepository` naming collision before
  writing any code; decided: new read-side interface is `SkuCatalogRepository`
  in a new `maximor/sku_mapping/` package, existing admin loader untouched.
- Read `maximor/worker/runner.py`, `jobs/dispatcher.py`, `jobs/contracts.py`,
  `jobs/repository.py`, `worker/__init__.py`, `worker/__main__.py`,
  `db/models/processing_job.py`, and migration `0006_analysis_job_link.py` to
  verify (not just recall from `PROJECT_NOTES.md`) the exact runner/dispatcher/
  handler contract and job-chaining mechanism — see the infra section above.
- Confirmed no SKU-mapping job type, handler, dispatcher entry, or linking
  migration exists yet; confirmed the foundation work in progress
  (`SkuMappingTask`, `SkuCatalogRepository`, `HybridSkuRetriever`) is pure
  library code with no worker/job awareness, to be wired up later by a future
  `SkuMappingHandler` that isn't part of this pass.
- Proposed the `maximor/sku_mapping/` skeleton (file layout, `SkuMappingTask`
  fields, Python-trigram retriever design, reserved semantic-source seam) to
  the user for review; awaiting go-ahead before writing files.

### 2026-09-05 — Run 3: Implemented the SKU-mapping foundation

- Renamed `maximor.catalog.repository.SkuRepository` -> `CatalogIngestionRepository`
  (and its one call site in `catalog/loader.py`); confirmed via grep it had no
  other callers or tests, so the rename was safe.
- Created `maximor/sku_mapping/` (new package):
  - `versions.py` — `SKU_MAPPING_TASK_SCHEMA_VERSION`,
    `SKU_RETRIEVAL_SCHEMA_VERSION`, `HYBRID_SKU_RETRIEVER_VERSION`.
  - `errors.py` — `SkuMappingError` base,
    `ActiveCatalogVersionNotFoundError`, `SkuMappingTaskConstructionError`.
  - `schemas.py` — `CatalogVersionRecord`, `SkuRecord`, `SkuMatchSource`
    (`exact`/`alias`/`lexical`/`semantic`), `RetrievedSku`, `SkuRetrievalResult`
    (carries `catalog_version_id` + `catalog_version_identifier` for
    reproducibility, per explicit instruction).
  - `contracts.py` — `SkuMappingTask` (keeps `candidate_evidence` and
    `commercial_status_evidence` as two labelled fields, not flattened; adds
    an `all_evidence` deduplicated convenience property); `build_sku_mapping_task`
    (constructs a task for one candidate + its exactly-one linked commercial
    status; deliberately makes **no eligibility decision** — that is left to a
    later, separately configurable policy); `SkuRepository` and
    `SemanticSkuSource` Protocols (the latter reserved, unused).
  - `repository.py` — `PostgresSkuRepository`, a read-only tenant-scoped
    implementation over the existing `catalog_versions`/`skus` tables
    (`get_active_catalog_version`, `list_active_skus`); performs no writes.
  - `retrieval.py` — `DeterministicHybridSkuRetriever`: exact/alias matching
    plus a **pure-Python character-trigram Dice-coefficient** lexical scorer,
    explicitly documented in its module docstring as the demo-scale lexical
    path, not the production one (production should push lexical matching
    into Postgres `pg_trgm` and add a real pgvector `SemanticSkuSource`).
    Accepts an unused `semantic_source` constructor arg as the reserved seam.
  - `__init__.py` — re-exports the above.
- Added three focused test files, all passing (20 tests, ~0.15s, no full-suite
  run): `test_sku_mapping_contracts.py`, `test_sku_mapping_retrieval.py`
  (includes one test against the real supplied 21-SKU `sku_catalog.json`
  fixture, read as data only), `test_sku_mapping_repository.py` (against the
  live test Postgres database, same pattern as
  `test_document_analysis_persistence.py`).
- Found and worked around (without modifying) an existing quirk in
  `DocumentAnalysisResult.identifiers_are_unique_and_ordered`
  (`document_analysis/schemas.py`): its generic per-collection id lookup
  checks attribute names in a fixed order or `("structure_id", "section_id",
  "term_id", "candidate_id", "assessment_id")`; since `CommercialStatusAssessment`
  has both `candidate_id` and `assessment_id`, `candidate_id` wins via
  `hasattr`, so uniqueness is actually enforced on `candidate_id` for the
  `commercial_statuses` collection instead of `assessment_id`. Side effect: a
  validated `DocumentAnalysisResult` can never contain two commercial-status
  assessments linked to the same candidate — so `build_sku_mapping_task`'s own
  "ambiguous multiple statuses" guard is currently unreachable through normal
  construction. Did not touch this (explicitly out of scope — no
  document-analysis behavior changes). The one test exercising that guard uses
  a minimal duck-typed `SimpleNamespace` stand-in instead of a real
  `DocumentAnalysisResult`, per explicit user direction not to work around the
  validator via `model_construct`.
- Verified the `SkuRepository` rename is clean: grep confirms the name now
  only appears inside `maximor/sku_mapping/`.
- Not done (explicitly out of scope for this pass, confirmed with user):
  `SkuMappingAgent`, final SKU decisions, mapping-decision persistence, any
  worker/job wiring (`JobType`, dispatcher, `build_worker()`, a linking
  migration), and eligibility-policy logic.

### 2026-09-05 — Run 4: Confirmed scope boundaries, opened eligibility-policy design

- User confirmed two standing decisions explicitly (no code changes needed,
  both already true): keep the defensive multiple-status guard in
  `build_sku_mapping_task`; do not modify `DocumentAnalysisResult` as part of
  this SKU-foundation ticket (the `candidate_id`/`assessment_id` id-lookup
  quirk noted in Run 3 stays untouched).
- User named the next open design decision: the eligibility policy — which
  `CommercialStatus` values actually get sent to SKU mapping (their example:
  maybe `purchased` + `included` map, `optional`/`excluded`/`mentioned` don't).
  Per instruction #3 from Run 3, this policy must NOT live inside
  `build_sku_mapping_task` — it is a separate, later, configurable component
  that decides which already-built tasks proceed to mapping.
- No code written this run; proposed a recommendation and raised the
  `ambiguous` status as the one case that may need a third outcome (route to
  review) rather than a simple map/skip boolean, and asked where the policy
  should live and how configurable it needs to be. Awaiting the user's
  decision before implementing.

### 2026-09-05 — Run 5: Walkthrough Q&A (no code), then finished the retriever

- Spent most of this run answering the user's questions about the code
  already written (`versions.py`, `contracts.py`, whether the supplied
  `sku_catalog.json` is the real mapping target, why `SkuRepository` returns a
  Pydantic `SkuRecord` rather than the raw SQLAlchemy `Sku` ORM model,
  end-to-end summary of what's implemented, where `document_id`/`candidate_id`
  come from, and the exact distinction between `SkuMappingTask` /
  `DeterministicHybridSkuRetriever` / the not-yet-built `SkuMappingAgent`). No
  source changes for any of that — pure explanation grounded in the actual
  code and its call chain.
- Confirmed `SkuMappingTask` is complete against the original spec; no changes
  made to it this run.
- Found and fixed a real completeness gap in `DeterministicHybridSkuRetriever`:
  the `semantic_source` constructor argument was accepted but never invoked
  inside `retrieve()` — a decorative, non-functional seam. Fixed:
  - Changed `SemanticSkuSource.search`'s return type in `contracts.py` from
    `tuple[SkuRecord, ...]` to `tuple[RetrievedSku, ...]`, since a semantic
    hit must carry a score to be merged fairly against exact/alias/lexical
    hits (documented explicitly in that Protocol's docstring).
  - `retrieval.py`: `retrieve()` now builds one `dict[uuid.UUID, RetrievedSku]`
    keyed by SKU id from lexical scoring, then — only if a `semantic_source`
    was actually supplied — calls it and merges each hit in via a new
    `_merge_into` helper (higher score wins, contributing sources are
    unioned, matched_text follows the higher score). Still zero embeddings,
    zero external calls: nothing in the codebase constructs a real
    `SemanticSkuSource`, so this path is currently exercised only by test
    doubles.
  - Added explicit `limit` bounds checking (`1..MAX_RETRIEVED_SKUS`) via a
    new `InvalidRetrievalLimitError` in `errors.py`, replacing what would
    otherwise have been a confusing Pydantic `ValidationError` surfacing from
    deep inside `SkuRetrievalResult` construction.
  - Exported `InvalidRetrievalLimitError` from `sku_mapping/__init__.py`.
- Added 3 tests to `test_sku_mapping_retrieval.py`: a semantic-only hit
  surfacing when lexical scoring finds nothing; a semantic hit merging with
  an existing lexical hit on the same SKU (sources union, score takes the
  max); and `limit` rejection at both bounds. All new tests use a plain
  in-test `_FakeSemanticSkuSource` stub — no real semantic search exists.
- Full focused suite (contracts + retrieval + repository) now 23/23 passing
  in ~0.17s.

### 2026-09-05 — Run 6: Real-catalog verification, scoping confirmation, agent design kickoff

- User asked whether `DeterministicHybridSkuRetriever` actually retrieves
  correctly, not just passes synthetic unit tests. Ran it live (no code
  changes) against the real 21-SKU catalog and the real product candidates
  from `tests/test_results/document_analysis/of-0001/attempt-9-result.json`:
  all 3 real candidates top-matched correctly, including correctly
  disambiguating `TALENT_ACQUISITION_CORE` from 4 close `TALENT_ACQUISITION*`
  siblings, and correctly resolving heavily abbreviated OCR-style text
  (`"Talent Intel. Platf. Prem. Ed. (Add-on)"`) to
  `TALENT_INTELLIGENCE_PLATFORM_PREMIUM_ADDON` — though that last one won by
  a narrow margin (0.62 vs 0.56) with lexical-only signal, worth remembering
  as a known-thin case, not a bug.
- Confirmed with the user, no code changes: keep the defensive
  multiple-status guard in `build_sku_mapping_task`; never modify
  `DocumentAnalysisResult`.
- Answered a scoping question: quantity/unit_price/dates/invoicing
  schedule/payment_terms/special_notes are explicitly NOT this subsystem's
  job — they live in the raw analysis output today (unnormalized strings in
  `ProductCandidate.raw_attributes`, passed through untouched by
  `SkuMappingTask`) and get parsed in the separate, not-yet-built
  Normalization stage per `PROJECT_NOTES.md`'s pipeline. Mapped every "Key
  Observations" bullet from `docs/Take-Home-assignment.pdf` to the pipeline
  stage that actually owns it — none of them land in SKU-mapping except
  "don't hallucinate a SKU that isn't there" (already reflected in the
  retriever's empty-result behavior).
- Corrected a misconception: `SkuMappingAgent` is not "invoked only when
  retriever confidence is low." Per `PROJECT_NOTES.md`, `retrieve_skus(...)`
  is one of the agent's own tools — the agent always runs per eligible task
  and always consults the retriever itself, mirroring how
  `DocumentAnalysisAgent` always uses its narrow tools. Investigated whether
  a confidence-based fast path (skip the LLM on a clean 1.0 exact match)
  would be safe: found two structural reasons it isn't guaranteed safe in
  general even though it held in the 3 spot-checked cases — `skus.name` has
  no DB uniqueness constraint (only `sku_code` does), so a tied 1.0 score
  is possible in principle; and a 1.0 score is a syntactic text-equality
  fact, not evidence examination, which `PROJECT_NOTES.md` explicitly assigns
  to the agent. User agreed: proceed with the settled architecture (agent
  always decides, no fast path for now).
- Read `document_analysis/agent.py` in full (1065 lines) as the direct
  template for `SkuMappingAgent` — the SDK session/finalization-capture/
  runtime-summary/guarded-finalizer machinery. Laid out the full set of
  mirrored pieces needed (decision output contract, toolset protocol,
  concrete adapter, guarded finalizer, the SDK agent class itself, a new
  skill file, validation module, versions, errors) and proposed building them
  incrementally rather than all at once, given the size.
- Settled the toolset shape: `SkuMappingToolset` will expose exactly three
  tools — `retrieve_skus`, `get_authoritative_sku`, and `get_evidence_region`.
  The third was not originally planned; added because `EvidenceReference` is
  a pointer (page/block id), not inline text — without a resolution tool the
  agent could never actually read the document text behind a candidate's
  evidence, only the pre-extracted `raw_name`/`raw_attributes` strings, which
  would contradict `PROJECT_NOTES.md`'s explicit "examines retrieved SKUs and
  evidence." Reusing `document_analysis`'s existing evidence-resolution logic
  rather than duplicating it. Deferred a broader `search_document`/
  `get_page_text` tool (for the "add document evidence / broaden retrieval
  text" retry behavior) to a later retry-focused pass, keeping the first
  version's toolset minimal.
- No code written this run — all of the above is confirmed design, not yet
  implemented.

### 2026-09-05 — Run 7: Building the SkuMappingAgent pieces one by one (A, B, C done)

User asked to build all seven planned pieces one by one:
A. Decision contract, B/C. Toolset+adapter, D. Guarded finalizer, E. SDK agent
class, F. Skill file, G. Validation module, H/I. Versions & errors.

- **A — done.** Added `SkuMappingOutcome` (`match`/`no_match`/`ambiguous`) and
  `SkuMappingDecision` to `sku_mapping/schemas.py`. Key design calls: `MATCH`
  requires `sku_id`+`sku_code`+`sku_name` together (redundant on purpose, so a
  later check can independently confirm all three agree with the
  authoritative catalog record); `AMBIGUOUS` requires `considered_sku_ids`
  with at least 2 distinct entries (naming what evidence couldn't
  distinguish, not just an unexplained shrug); **every** outcome — including
  `NO_MATCH`/`AMBIGUOUS` — requires at least one evidence reference, since
  those are still material conclusions about the candidate, not an absence of
  one. Added `SKU_MAPPING_DECISION_SCHEMA_VERSION` to `versions.py`.
  `test_sku_mapping_decision.py`: 8 tests, all passing.
- **B/C — done.** Added `sku_mapping/tool_schemas.py`
  (`RetrieveSkusInput`, `GetAuthoritativeSkuInput`) and the `SkuMappingToolset`
  + `EvidenceResolver` Protocols to `contracts.py`. One real design decision
  worth remembering: `RetrieveSkusInput` deliberately carries **no free-text
  query** — unlike `document_analysis`'s `search_document` (which explores an
  unknown document), the mapping agent is always scoped to one fixed
  `SkuMappingTask`; letting the LLM supply its own query text would let it
  search the catalog with text disconnected from the candidate's actual
  evidence-grounded `raw_name`/`raw_attributes`. Implemented
  `PersistedSkuMappingTools` (`tools.py`): one instance is built for exactly
  one `SkuMappingTask`; `get_authoritative_sku` is scoped to whichever
  catalog version *this instance's own* `retrieve_skus` call actually used
  (tracked as instance state), not independently re-resolved, and calling it
  before any retrieval raises a new `AuthoritativeLookupBeforeRetrievalError`
  — added specifically to guarantee retrieval and authoritative confirmation
  within one decision can never silently disagree about which catalog
  snapshot was consulted. `get_evidence_region` delegates to a narrow
  `EvidenceResolver` (reuses `document_analysis`'s existing evidence
  resolution — not reimplemented). Added `SkuNotFoundError` too.
  `test_sku_mapping_tools.py`: 7 tests, all passing (fakes only, no DB —
  each real collaborator is already tested elsewhere).
- Full `sku_mapping` suite now 38/38 passing.
- Next: D (guarded finalizer) + G (validation module) together, since
  validation defines what the finalizer checks — then F (skill file), then
  E (the SDK agent class) last.
- **Structural correction found while starting D/G:** re-checked
  `document_analysis/agent.py` and there is no standalone finalizer module —
  `_finalizer_input_schema`, `_validate_structured_output`,
  `_trusted_result_fields`, and the `finalize` tool closure are all built
  directly inside `ClaudeDocumentAnalysisAgent` itself, calling out to the
  separately-tested `document_analysis/validation.py`. So "D. Guarded
  finalizer" is not a separate file in this codebase's actual pattern — it is
  logic that lives inside "E. The SDK agent class." Folded D into E rather
  than inventing an artificial extra module just to preserve a literal
  seven-file count; flagged this explicitly to the user rather than silently
  changing the plan.
- **G — done.** Added `sku_mapping/validation.py`: `SkuMappingValidationIssue`
  dataclass and `validate_sku_mapping_decision(task, decision, runtime)`, a
  pure function mirroring `document_analysis/validation.py` exactly (no DB,
  no Claude, safe structured issues only). Key design choice: for a `MATCH`
  decision, validation does NOT re-query the database to confirm
  `sku_id`/`sku_code`/`sku_name` agree with the authoritative catalog record —
  it cross-checks against a `SkuMappingCompletionRuntime.authoritative_skus_by_id`
  dict the future SDK harness (Part E) must populate from its own successful
  `get_authoritative_sku` tool calls this same invocation. This mirrors
  `document_analysis`'s "trust a successful narrow-tool execution, don't
  re-fetch" pattern. Checks implemented: trusted identity match, schema
  version match, `retrieve_skus` was actually called at least once (not
  correctable — mirrors `document_analysis`'s "overview_not_retrieved"
  precedent: should never happen if the prompt is followed, treated as a
  structural anomaly, not a fixable content mistake), decision evidence must
  literally be a subset of the task's own `all_evidence` (correctable — the
  agent can just cite real evidence instead), and for `MATCH` specifically:
  `get_authoritative_sku` was called, the claimed `sku_id` was actually
  confirmed this invocation (correctable), and the confirmed record's
  `sku_code`/`sku_name`/organization/catalog_version/active-status all agree
  with what the decision claims (all four NOT correctable — a real integrity
  failure should prompt full reconsideration, not a quick JSON patch).
  `test_sku_mapping_validation.py`: 12 tests, all passing.
- Full `sku_mapping` suite now 50/50 passing.
- Next: F (skill file), then E (the SDK agent class, which will also contain
  the finalizer logic that was originally labeled D).

### 2026-09-05 — Run 8: F (skill file) and E (ClaudeSkuMappingAgent) — all seven parts done

- **F — done.** Added `.claude/skills/sku-mapping/SKILL.md`, mirroring
  `order-form-analysis/SKILL.md`'s tone/structure. Key content: always call
  `retrieve_skus` first (it takes no search text — grounds retrieval in the
  candidate's own evidence, not agent-chosen text); a low/empty retrieval is
  not proof of no match — use `get_evidence_region` before concluding
  `NO_MATCH`; confirm the intended SKU with `get_authoritative_sku` before
  proposing `MATCH`; never call `get_authoritative_sku` before `retrieve_skus`;
  every outcome needs evidence already belonging to the candidate; explicitly
  out of scope: money/date arithmetic, other attribute normalization,
  reconsidering commercial status. Named the finalizer tool
  `submit_sku_mapping_decision`. Added `SKU_MAPPING_SKILL_VERSION`,
  `SKU_MAPPING_PROMPT_VERSION`, `SKU_MAPPING_AGENT_VERSION` to `versions.py`.
- Before starting E, confirmed with the user (via question, not assumption)
  how much of `DocumentAnalysisRuntimeSummary`'s diagnostics fidelity to
  match — chose **full mirror**, all ~50 fields including the bounded
  timing-event/tool-diagnostics arrays, not just the load-bearing subset.
- Added infra E depends on that wasn't originally called out as one of the
  seven parts, flagged explicitly rather than done silently:
  `config.py` gained `sku_mapping_model`/`sku_mapping_max_turns`/
  `sku_mapping_max_thinking_tokens`/`sku_mapping_timeout_seconds`/
  `sku_mapping_max_budget_usd`/`sku_mapping_max_corrections`/
  `sku_mapping_project_root`, mirroring the `document_analysis_*` settings
  exactly (smaller defaults: 8 turns/2048 thinking tokens/60s timeout, since
  this agent's job is narrower). `.env.example` got matching entries.
  `errors.py` gained a `SkuMappingToolError` base (the four existing tool
  errors now inherit from it, no behavior change) plus
  `SkuMappingNotConfiguredError`, `SkuMappingConfigurationError`,
  `SkuMappingRuntimeError`, `SkuMappingValidationError` — full 1:1 naming
  parity with `document_analysis/errors.py`.
- **E — done.** Added `sku_mapping/agent.py`: `SkuMappingAgent` Protocol
  (method `decide`, not `analyze` — matches PROJECT_NOTES.md's own verb),
  `UnconfiguredSkuMappingAgent`, `SkuMappingRuntimeSummary` (full mirror plus
  two genuinely new fields: `catalog_version_id` and
  `authoritative_skus_by_id`, both required by `validate_sku_mapping_decision`),
  `SkuMappingExecution`, and `ClaudeSkuMappingAgent` — a structural mirror of
  `ClaudeDocumentAnalysisAgent` (same session lifecycle, finalization-capture
  event system, timeout/cancellation handling, one-correction retry rule).
  Real design differences from the template, each reasoned through rather
  than copied blindly:
  - **The prompt embeds the candidate's own content directly**
    (`raw_name`, `raw_attributes`, `commercial_status`, evidence locations) —
    `document_analysis`'s prompt is identifier-only because content lives
    behind tools; `SkuMappingTask` already carries its (small, bounded)
    content inline, so there is no tool that would let the agent "discover"
    it, and the agent needs it to reason at all.
  - **`retrieve_skus` and `get_authoritative_sku` take no trusted org/run
    kwargs** the way `document_analysis`'s uniform `ToolScope`-based
    injection does — those two input models don't extend `ToolScope` at all
    (only `get_evidence_region`'s does, reusing `EvidenceRegionInput`
    unchanged), because the adapter itself is already bound to one fixed
    task/organization. Generalized the generic `invoke()` helper to accept
    per-tool `trusted_kwargs` instead of injecting the same two keys
    everywhere.
  - **`catalog_version_id` is trusted-injected from the runtime, not the
    task** — `_trusted_result_fields(task, runtime)` (note: two args, not
    one) pulls it from `runtime.catalog_version_id`, which is only set once
    this invocation's own `retrieve_skus` call actually succeeds. A
    finalizer call before any retrieval degrades to a clean pydantic
    rejection (not correctable) rather than a special-cased error path.
  - `_map_pydantic_error` maps `SkuMappingDecision`'s own seven validator
    messages (match/ambiguous field-consistency, evidence requirements) to
    stable codes, replacing `document_analysis`'s document-shaped mappings.
  - No `_normalize_structured_order` equivalent — `SkuMappingDecision` has no
    multi-collection sorted-ID requirement the way `DocumentAnalysisResult`
    does, so that whole method has no analog here.
- Tests: `test_sku_mapping_agent.py`, 25 tests, all passing — mirrors
  `test_claude_document_analysis_agent.py`'s `FakeClient`-injection pattern
  (zero live API calls, zero network/subprocess): configuration-error
  non-disclosure, options/tool-discovery shape, finalizer schema field
  stripping, pydantic-error-code mapping, direct adapter invocation
  (including the new `catalog_version_id`/`authoritative_skus_by_id`
  capture), full happy-path execution via a fake SDK client, MCP
  failure/misregistration handling, one-correction-then-success and
  second-failure-stops-cold flows, non-correctable trusted-override and
  missing-retrieval flows, usage/cost accumulation, terminal-failure code
  mapping, and a real `asyncio.timeout` firing.
- Full verification this run: `sku_mapping` suite 75/75, `document_analysis`
  suite 62/62 (no regression from the shared `config.py`/`errors.py`/catalog
  rename changes), `test_api.py` + `test_worker.py` 24/24. **161/161 total,
  zero live Claude API calls made.**
- All seven planned pieces (A–I) are now implemented: `SkuMappingTask`,
  `SkuRepository`/`PostgresSkuRepository`, `HybridSkuRetriever`/
  `DeterministicHybridSkuRetriever`, `SkuMappingDecision`, the three-tool
  `SkuMappingToolset`/`PersistedSkuMappingTools`, `validate_sku_mapping_decision`,
  the `sku-mapping` skill, and `ClaudeSkuMappingAgent` itself.
- **Still not done, deliberately out of scope, confirmed earlier in this
  conversation:** worker/job wiring (no `JobType.SKU_MAPPING`, no
  `SkuMappingHandler`, no `build_worker()` registration, no
  `schedule_sku_mapping_job`, no linking migration), mapping-decision
  persistence, the eligibility policy (which `CommercialStatus` values
  actually get a task built and sent through this agent at all — flagged as
  the next real design decision several runs ago and still unresolved), and
  any evaluation against the real 50-document ground-truth set.
