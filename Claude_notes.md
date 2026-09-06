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

### 2026-09-05 — Run 9: First real, paid Claude call through the whole subsystem

- User asked to backburner worker/job wiring (two real forks surfaced there:
  job-to-candidate linking needs a compound `(analysis_run_id, candidate_id)`
  key since one analysis run has many candidates, unlike the existing
  `preprocessing_run_id`-only chaining pattern; and auto-chaining is blocked
  on the still-unresolved eligibility policy) and instead run the real
  `ClaudeSkuMappingAgent` end to end against `of-0001`'s actual persisted
  analysis output, before touching worker wiring at all.
- **Data prep, not just code:** discovered `of-0001`'s document belongs to
  organization `preprocess-demo-fa422e41721d` (`fa422e41-...`), a
  preprocessing-test org with **zero SKUs / no active catalog version** — a
  different org from the one holding the real 21-SKU catalog. Loaded the real
  catalog into *that* org (`catalog/loader.py`, same slug, upserts in place)
  so retrieval could run against real persisted document + real catalog data
  together. Confirmed 124 real `document_blocks` exist for its preprocessing
  run (needed for real `get_evidence_region` resolution) and identified the
  correct analysis run (`82385e20-...`, 3 real candidates — matches the
  `of-0001/attempt-9-result.json` fixture used throughout this project).
- Wrote a one-off script (`run_sku_mapping_live.py`, in the job scratch dir,
  not part of the app) wiring the *real* `PostgresSkuRepository`,
  `DeterministicHybridSkuRetriever`, real `PersistedDocumentTools` (for
  evidence), `PersistedSkuMappingTools`, and a real `ClaudeSkuMappingAgent`
  (no fake `client_factory`) against that data.
- **First real run: all 3 candidates failed.** Found and fixed two genuine
  bugs, each verified by re-running against real Claude, not by inspection
  alone:
  1. `_prompt()` never told the agent `task.preprocessing_run_id`, but the
     agent needed to echo that value inside each submitted evidence object
     (a nested required field, not a top-level trusted field it can't touch).
     Every submission failed `decision_evidence_run_mismatch`, and since the
     agent had no way to *learn* the correct value, it burned its whole
     turn/time budget trying to "correct" an uncorrectable error
     (`sku_mapping_max_turns` / `sku_mapping_timeout`).
  2. After that fix, a deeper issue: the finalizer required the agent to
     resubmit a full structured `EvidenceReference` object
     (`representation`, `extraction_source`, `bounding_box`, ...)
     reconstructed from a compact prompt summary that only ever showed
     `page_number`+`block_id`. Even a genuinely correct citation failed
     exact-equality against the task's stored evidence. **Fixed
     architecturally, not by adding more prompt detail:** the finalizer
     schema now exposes `evidence_ids` (plain `block_id`/`table_id` strings,
     already shown in the prompt) instead of `evidence`; a new
     `_resolve_evidence_ids` static method on `ClaudeSkuMappingAgent` looks
     each identifier up against `task.all_evidence` and substitutes the
     exact stored object server-side before validation. This makes the
     whole failure class structurally impossible rather than papering over
     one instance of it. Unresolvable/malformed identifiers reject as
     correctable (`evidence_identifier_unresolved`).
  - Added regression tests for both: `test_prompt_explains_evidence_id_citation`,
    `test_resolve_evidence_ids_returns_exact_task_evidence`,
    `test_resolve_evidence_ids_rejects_anything_unresolvable`, updated
    `test_finalizer_schema_excludes_all_trusted_fields` to check `evidence`
    is gone / `evidence_ids` is required. Updated `valid_decision_payload`
    test fixture to the new shape. Full `test_sku_mapping_agent.py`: 31/31
    passing (no live call in the test suite itself).
- **Second run (all 3 real candidates): full success.** ~$0.086 total,
  5 turns / ~$0.02–0.04 each:
  - `pc_talent_acquisition_core` (purchased) → `MATCH` `TALENT_ACQUISITION_CORE`
    — matches the deterministic-retriever spot check from Run 6.
  - `pc_talent_intel_platform_prem` (purchased, abbreviated OCR-style text)
    → `MATCH` `TALENT_INTELLIGENCE_PLATFORM_PREMIUM_ADDON` — the retriever's
    own score here was a thin 0.619 vs. 0.556 for the wrong sibling (flagged
    as a known-thin case in Run 6); the LLM correctly reasoned through the
    abbreviation ("Talent Intel. Platf. Prem. Ed." = "Talent Intelligence
    Platform Premium Edition") rather than just trusting the score — real
    evidence the two-stage (deterministic shortlist + LLM judgment)
    architecture earns its complexity.
  - `pc_professional_service` (**ambiguous** commercial status) → `MATCH`
    `PROFESSIONAL_SERVICE`, with the rationale explicitly noting "commercial
    status ambiguity is noted... but is not reconsidered here" — correct
    per the skill's instruction not to re-litigate commercial status, and a
    concrete real-data illustration of why the eligibility-policy question
    is real: this candidate now has a clean SKU match sitting on top of an
    unresolved question of whether it should count as a purchase at all.
  - All three needed exactly one correction round before succeeding — a
    suspiciously uniform pattern worth investigating later (unconfirmed
    hypothesis: possible case-sensitivity on the `outcome` enum, e.g. the
    model first submitting `"MATCH"` before `"match"` — not yet verified
    against actual first-attempt rejection data, since the runtime only
    retains the latest attempt's validation state).
- Net: the subsystem's actual decision quality checked out against real
  Claude on real data for all 3 available candidates, including a hard case
  and an ambiguous-status case. Two real, non-obvious bugs were caught and
  fixed specifically *because* this was tested live rather than only via
  `FakeClient`-based unit tests — worth remembering as a case for live
  smoke-testing before wiring anything into production paths.

### 2026-09-05 — Run 10: Confirmed decision-storage gap; three concerns queued, all paused

- Confirmed precisely (via `getattr`/printed fields, no fabrication) what a
  real `SkuMappingDecision` looks like and, more importantly, where it's
  stored: **nowhere**. No table, no object-storage artifact — the object
  lived only in the scratch script's process memory and was discarded on
  exit. This is the direct, concrete consequence of the still-open
  "decision persistence" scoping question from Run 8/9.
- Confirmed (with real evidence, not speculation) a second real gap the user
  flagged independently: per-candidate numeric facts (`extended_price`,
  `qty`, `unit_fee`) are already cleanly scoped to `candidate_id` in
  `ProductCandidate.raw_attributes`, so normalizing those per mapped SKU is
  straightforward — but document-wide facts (dates, payment terms,
  invoicing frequency) are captured as `GlobalTerm` entries with **no
  candidate-linking field at all** (`GlobalTerm` has `term_id`/`raw_name`/
  `raw_value`/`evidence` — nothing else). Verified this directly against
  `of-0001`'s real `global_terms` (e.g. `gt_end_date: "31 Jul 2027"`) — genuinely
  unresolved anywhere in the pipeline, matching the assignment's own warning
  that different contract items can have different terms. Flagged that
  fixing this properly is a `document_analysis`-side schema change (adding
  candidate-attribution to `GlobalTerm`), which is a different subsystem
  than SKU-mapping and has been treated as off-limits this whole session —
  did not propose doing it, just named it as a separate future conversation.
- User consolidated the remaining work into three named concerns and asked
  for a detailed, self-contained write-up of each (to paste into a separate
  ChatGPT conversation that has broader context on this system's overall
  design/architecture history). Full detail below — this is the actual
  content handed to the user, not a summary of it.
- **User has not decided and is deferring the decision to that other
  conversation**, and will come back with a direction. **All three concerns
  are paused. No code was written this run.** Next session should wait for
  the user's direction before starting (1), (3), or opening the
  (2)/`document_analysis` conversation.

#### Concern 1 — Where and how should `SkuMappingDecision` be persisted?

The SKU-mapping subsystem is fully built and verified end-to-end with real
paid Claude API calls against real order-form data: `SkuMappingTask`,
`SkuRepository`/`PostgresSkuRepository`, `HybridSkuRetriever`/
`DeterministicHybridSkuRetriever`, `SkuMappingDecision`, a three-tool
`SkuMappingToolset` (`retrieve_skus`, `get_authoritative_sku`,
`get_evidence_region`), `validate_sku_mapping_decision`, a `sku-mapping`
Claude Agent SDK skill, and `ClaudeSkuMappingAgent` (full SDK harness:
session lifecycle, guarded finalizer, one-correction retry, bounded runtime
diagnostics). It correctly mapped all 3 real candidates from a test order
form, including one with abbreviated/OCR-style text and one with an
ambiguous commercial status.

The gap: **zero persistence**. `SkuMappingDecision` exists only in memory
for one process invocation and is discarded on exit — no table, no
object-storage artifact.

Existing precedent to weigh against — `document_analysis` already has a
full persistence layer: `document_analysis_runs` table (one row per
attempt: status, model, tokens, cost, turn count, a
`canonical_result_storage_key` pointing to a compressed JSON artifact in
object storage, with a DB check constraint enforcing that `status='completed'`
requires the artifact/checksums/validation to all be present);
`DocumentAnalysisPersistenceService` (`create_run()` /
`save_completed_result()` — validates before persisting, atomic, cleans up
orphaned storage on failure — / `load_completed_result()` — re-validates
checksums and identity on read); relational projections
(`document_product_candidates`, `document_commercial_status_assessments`,
`document_analysis_evidence_references`) mirroring the same data into
queryable SQL alongside the canonical artifact.

Decisions needed:
1. Full treatment mirroring `document_analysis` (a `sku_mapping_runs`-style
   table + persistence service + canonical artifact + relational
   projections), or something lighter (one simple table, accepted decision
   fields only, no run/attempt tracking, no runtime diagnostics)?
2. Should failed/rejected attempts be persisted at all, or only accepted
   decisions?
3. Should the stored record capture `catalog_version_id` for reproducibility
   (trace a decision back to the exact catalog snapshot that produced it)?
4. Should decisions be versioned/re-computable — if the catalog changes
   after a decision was made, keep the old decision as historical record, or
   do something else (revalidate, flag as stale)?

#### Concern 2 — How do we link document-wide facts (dates, payment terms, invoicing frequency) to the correct SKU?

`DocumentAnalysisResult` splits extracted facts into two shapes:
`ProductCandidate.raw_attributes` (a `dict[str, str | None]` scoped to one
`candidate_id` — verified in real data to correctly hold per-item facts like
`extended_price`, `qty`, `unit_fee`, `sku_description`, already 1:1 tied to
the candidate that will get SKU-mapped); and `GlobalTerm`
(`{term_id, raw_name, raw_value, evidence}`) — **document-wide facts with no
field linking them to any specific candidate_id at all.**

Verified concretely against real data (`of-0001`): `GlobalTerm` entries
included `"End Date": "31 Jul 2027"`, `"Order Form Effective Date"`, an
employee-size cap, and governing-agreement text — none referencing which of
the document's 3 product candidates they apply to.

Why it matters: the take-home assignment's own brief explicitly warns
*"different contract items can have different terms"* — you cannot safely
assume one global term (e.g. a service end date) applies uniformly to every
mapped SKU. Nothing in the pipeline currently resolves this attribution
question — it is a genuine, currently-unaddressed gap, not handled
elsewhere.

Three candidate approaches:
1. **Fix it upstream in `DocumentAnalysisAgent`'s own output schema** — have
   it explicitly record which `candidate_id`(s) each `GlobalTerm` applies to
   (e.g. an `applies_to_candidate_ids: tuple[str, ...] | None` field), since
   that agent already has full document context to judge this. A
   schema/behavior change to `document_analysis`.
2. **Defer to a later Normalization stage** — leave `document_analysis`'s
   output unchanged; a separate, not-yet-designed Normalization step
   re-derives the linkage by reasoning over `global_terms` +
   `product_candidates` + evidence together, after SKU mapping.
3. **A deterministic-default hybrid** — assume a global term applies to all
   candidates unless a candidate's own `raw_attributes` explicitly overrides
   it, falling back to a narrow LLM judgment call only on an actual
   conflict.

Also worth deciding: should this be part of the SKU-mapping subsystem's
responsibility at all, or strictly owned by a separate, not-yet-built
Normalization subsystem?

#### Concern 3 — How should worker/job infrastructure connect `SkuMappingAgent` to candidates as they're produced by `DocumentAnalysisAgent`?

Existing infrastructure pattern: a generic runner/dispatcher/handler
architecture. `WorkerRunner` polls `processing_jobs`
(`SELECT ... FOR UPDATE SKIP LOCKED`, safe for concurrent workers),
`JobDispatcher` routes by `job_type` to a registered `JobHandler`
(`async def execute(context) -> None`, must raise on failure), and **only
the runner** ever sets a job's terminal status — handlers never touch that
themselves, though a handler may own its own domain-specific run-status row.
Stage-chaining is a handler side effect on success:
`DocumentPreprocessingHandler` calls `schedule_document_analysis_job(...)`
after completing, an idempotent create-or-return guarded by a partial
unique index on `processing_jobs` scoped to `job_type='document_analysis'`.

Why SKU-mapping's chaining is structurally different, not a copy-paste: one
`document_analysis_run` produces *multiple* product candidates (not 1:1 like
preprocessing→analysis), and the design is one `SkuMappingTask`/decision
**per candidate**. So a `sku_mapping` job must be keyed by the compound pair
`(analysis_run_id, candidate_id)`, not a single foreign key — needs two new
nullable columns on `processing_jobs` plus a compound partial-unique index
(extending the existing pattern, not reusing it as-is).

The blocking design question — eligibility: should `DocumentAnalysisHandler`
automatically schedule a `sku_mapping` job for every candidate the instant
analysis completes? Candidates carry a `CommercialStatus` (`purchased` /
`included` / `optional` / `excluded` / `mentioned` / `ambiguous`), and which
of these should actually be sent to SKU mapping has been deliberately left
undecided all session — auto-chaining without that decision means silently
baking in an unreviewed default (e.g. "only `purchased`").

Decisions needed:
1. Exact job-to-candidate linking migration/schema (new columns, index
   shape, FK to `document_analysis_runs`)?
2. Automatic scheduling (`DocumentAnalysisHandler` auto-chains on
   completion, using some explicit provisional eligibility default) vs.
   manual (a standalone `schedule_sku_mapping_job(analysis_run_id,
   candidate_id)` function, invoked explicitly, until the eligibility policy
   is properly settled)?
3. What should `SkuMappingHandler` actually do with the decision once the
   agent returns it — connects directly to Concern 1 (persistence).

### 2026-09-06 — Run 11: Concern 1 (persistence) settled via external design conversation

- User returned with a full, detailed persistence design for Concern 1
  (decided in a separate ChatGPT conversation with broader system-design
  context, per Run 10). No code written yet — this run is decision capture.
- Full design: `sku_mapping_runs` table mirroring `document_analysis_runs`
  (org/document/analysis-run identifiers, versions, `catalog_version_id`,
  token/cost diagnostics, validation status); one compressed canonical JSON
  artifact per **accepted** run (task identity, retrieved shortlist,
  decision, evidence ids, catalog version identity); relational projections
  for accepted decisions only; failed/rejected attempts persisted as
  metadata-only, never as an accepted artifact — matching
  `document_analysis_runs`'s existing discipline exactly.
- Two refinements from the user that are real, non-obvious improvements over
  what I'd have defaulted to on my own:
  1. **Derived supersession, not mutation.** A catalog change produces a new
     `sku_mapping_runs` row carrying `supersedes_mapping_run_id →
     old_run.id` (explicit forward lineage). The old accepted row's
     `status` never changes from `completed` — "superseded" is *derived*
     (does any row point back at this one), not a field flipped on the old
     row. Persistence service must enforce the replacement shares the same
     organization/document/analysis_run/candidate as what it supersedes.
  2. **`document_product_candidate_id` (UUID FK to `document_product_candidates.id`)
     is the linking key on `sku_mapping_runs`**, not the external string
     `candidate_id` (e.g. `pc_talent_acquisition_core`) `SkuMappingTask`
     itself uses everywhere. The external string stays inside the canonical
     artifact/projection as reference data only. This means the future
     persistence service must resolve `(analysis_run_id, external_candidate_id)`
     → `document_product_candidates.id` before creating a `sku_mapping_runs`
     row — a real new step, not yet built anywhere.
- Added a new "## Decision Log" section to `PROJECT_NOTES.md` (dated
  2026-09-06, appended at the end, no existing content touched) capturing
  this settled decision in full, per explicit instruction and per that
  file's own working agreement ("keep an explicit decision log for settled
  architectural choices"). This is the first entry in that file's decision
  log; `PROJECT_NOTES.md` had not been edited by Claude at all before this
  run, only read.
- Concerns 2 and 3 remain open/paused, explicitly noted as such in the new
  `PROJECT_NOTES.md` section too. Have not yet asked/confirmed whether to
  start implementing Concern 1 now or wait for 2/3 — next step is to ask
  that, not assume.

### 2026-09-06 — Run 12: Implemented Concern 1 (full SKU-mapping persistence)

User said "implement now." Built the full settled design from Run 11 as six
tracked sub-tasks. All 237 tests in the entire suite pass afterward (91
sku_mapping + 146 everything else), zero regressions, migration verified to
upgrade/downgrade/re-upgrade cleanly.

- **Runtime addition (necessary prerequisite, not originally one of the 7
  parts):** `SkuMappingRuntimeSummary` gained `last_retrieval_result:
  SkuRetrievalResult | None`, captured in `agent.py`'s `invoke()` alongside
  the existing `catalog_version_id` capture — needed because the canonical
  artifact must bundle task+retrieval+decision together, and nothing
  previously retained the retrieval result after the tool call returned.
- **`SkuMappingRunArtifact`** (new, in `contracts.py` — not `schemas.py`,
  to avoid a circular import since it must reference `SkuMappingTask`):
  bundles `task` + `retrieval` + `decision`, with a validator requiring all
  three to agree on `candidate_id`/`organization_id`, and requiring
  `retrieval.catalog_version_id == decision.catalog_version_id`. New
  `SKU_MAPPING_ARTIFACT_SCHEMA_VERSION` constant.
- **DB models** (`db/models/sku_mapping.py`, new): `SkuMappingRun` mirrors
  `document_analysis_runs` field-for-field, plus the settled adjustments —
  `document_product_candidate_id` (FK to `document_product_candidates.id`,
  not the external string), `catalog_version_id` (FK to `catalog_versions.id`,
  **nullable** — a real design gap I caught while building, not in the
  original spec: unlike `preprocessing_run_id` in `document_analysis_runs`,
  it genuinely isn't known until this run's own `retrieve_skus` call
  succeeds mid-execution, so it can't be `NOT NULL` from creation; the
  `completed_result_required` check constraint requires it non-null only at
  `status='completed'`), and `supersedes_mapping_run_id` (self-FK, forward
  lineage per the settled design). `SkuMappingDecisionProjection` (one row
  per accepted run) and `SkuMappingEvidenceReference` (had to drop
  "decision" from the **table** name specifically — `sku_mapping_decision_evidence_references`
  combined with a column like `preprocessing_run_id` exceeds PostgreSQL's
  63-character identifier limit for the auto-generated index name; the
  Python class name still says `SkuMappingEvidenceReference` too, kept
  consistent).
- **Migration `0009_sku_mapping_persistence.py`**: hit and fixed two real
  bugs before it applied cleanly — a `server_default` for a JSONB column
  needs `sa.text("'[]'::jsonb")`, not a raw Python string (raw string got
  double-quoted by SQLAlchemy, invalid JSON); and the identifier-length
  issue above. Amended the migration in place (not a follow-up migration)
  for the nullable `catalog_version_id` fix since it had zero data riding on
  it yet. Verified upgrade → downgrade → upgrade round-trips clean.
- **`SkuMappingPersistenceService`** (`persistence.py`, new): mirrors
  `DocumentAnalysisPersistenceService`'s shape (`create_run` /
  `save_completed_result` / `mark_run_failed` / `load_completed_result` /
  `latest_run`) with three real additions beyond a straight port:
  1. `create_run` takes the **external** `candidate_id` string (what
     `SkuMappingTask` actually carries) and resolves it to
     `document_product_candidates.id` internally via `_validate_source` —
     the "real new step, not yet built anywhere" flagged back in Run 11.
  2. When `supersedes_mapping_run_id` is given, `_validate_supersedes`
     enforces the referenced run shares this run's organization/document/
     analysis_run/candidate, exactly as instructed.
  3. `save_completed_result` re-verifies a `MATCH` decision's SKU against
     the **live** `skus` table (`_resolve_sku`) as a second, independent
     check beyond the pure `validate_sku_mapping_decision` gate — catches
     staleness between when the agent confirmed a SKU and when persistence
     actually runs (e.g. the SKU was deactivated in between), which a
     DB-free validator structurally cannot see. Added
     `SkuMappingPersistenceError`.
- **Tests** (`test_sku_mapping_persistence.py`, 7, all passing): round-trip
  success with both projections verified in SQL; failed-run diagnostics;
  completion-gate rejection; the live-staleness SKU re-check (a decision
  that's internally consistent with its own runtime, so the pure validator
  passes, but the live `Sku` row was deactivated after — this is the one
  that actually exercises `_resolve_sku`, not the same case the pure
  validator already covers); duplicate attempt-number rejection; unknown-
  candidate rejection; supersedes lineage enforcement (both the allowed
  same-lineage case and the rejected cross-lineage case). Hit and fixed
  three test-setup bugs along the way (`LocalObjectStorage` needs a `Path`
  not a `str`; the shared test-document-checksum collided across two
  `_source()` calls in one test; an organization can only have one *active*
  catalog version, so a second unrelated `_source()` call needed a
  `with_catalog=False` escape hatch) — none were service bugs.
- Concerns 2 (global-term-to-SKU linking) and 3 (worker/job wiring) remain
  open and untouched this run, as agreed.

### 2026-09-06 — Run 13: Implemented Concern 3 (worker/job wiring) on top of Run 12's persistence

User's framing: Concern 3 depends on Concern 1 — a handler cannot safely
complete if it has nowhere durable to save the decision — so this run first
re-verified Run 12's persistence against the newly re-specified requirements
(matched exactly, no changes needed), then built the full worker-integration
layer in one pass. Explicit hard constraints carried forward and honored: no
live Claude calls, no reopening PDFs, no touching ground truth/supplied data,
no full-suite runs (focused test files only).

- **`JobType.SKU_MAPPING`** added to `jobs/types.py`.
- **`processing_jobs` schema** (`db/models/processing_job.py`, full rewrite):
  two new nullable columns, `analysis_run_id` and
  `document_product_candidate_id` (plain columns — no per-column FK, both
  covered by composite FKs instead); `CheckConstraint
  sku_mapping_link_required` enforcing both fields set together iff
  `job_type='sku_mapping'`; two composite `ForeignKeyConstraint`s —
  `(organization_id, document_id, analysis_run_id) →
  document_analysis_runs` and `(organization_id, analysis_run_id,
  document_product_candidate_id) → document_product_candidates` — chosen
  over per-column FKs specifically so a job cannot reference another
  tenant's or another document's analysis run/candidate, not just an
  arbitrary valid UUID; a partial unique `Index` on
  `(analysis_run_id, document_product_candidate_id)` scoped to
  `job_type='sku_mapping'` and non-terminal status, preventing concurrent
  duplicate jobs for the same candidate while still allowing a fresh
  attempt after a terminal one (same shape as the existing
  `schedule_document_analysis_job` idempotency pattern). Needed two new
  supporting composite-unique constraints on `document_analysis_runs` and
  `document_product_candidates` (`db/models/document_analysis.py`) purely
  so the composite FKs above have something to point at.
- **Migration `0010_sku_mapping_job_link.py`** (`down_revision = "0009_sku_mapping_persistence"`):
  one real bug — `sa.dialects.postgresql.UUID` doesn't exist via a bare
  `import sqlalchemy as sa`; needed `from sqlalchemy.dialects import
  postgresql` and `postgresql.UUID(as_uuid=True)` explicitly. Verified
  upgrade → downgrade → upgrade round-trip clean, then applied for real
  (`alembic -c alembic.ini upgrade head`, confirmed at
  `0010_sku_mapping_job_link (head)`).
- **`SkuMappingEligibilityPolicy`** (`sku_mapping/eligibility.py`, new):
  versioned (`SKU_MAPPING_ELIGIBILITY_POLICY_VERSION = "1.0.0"`), pure
  lookup table from `CommercialStatus` to an `EligibilityDisposition`
  (`SCHEDULE`/`SKIP`/`REVIEW`) — `purchased`/`included` → schedule;
  `optional`/`excluded`/`mentioned` → skip; `ambiguous` → review, which is
  deliberately **not** schedule (an ambiguous commercial status shouldn't
  silently trigger a SKU lookup that presumes the item was ordered).
- **`schedule_sku_mapping_job`** (`jobs/service.py`, new function): mirrors
  `schedule_document_analysis_job`'s idempotent create-or-return +
  `IntegrityError`-fallback pattern exactly, keyed by
  `(analysis_run_id, document_product_candidate_id)` instead of
  `(document_id,)`.
- **`DocumentAnalysisHandler`** (`worker/handlers/document_analysis.py`):
  gained an injectable `eligibility: SkuMappingEligibilityPolicy | None`
  constructor param, and a new `_schedule_eligible_sku_mapping_jobs` step
  called only *after* the existing persist-then-reload-validate check
  succeeds. Deliberately reads the just-written
  `document_product_candidates`/`document_commercial_status_assessments`
  rows back from the DB rather than using the in-memory `execution.result`
  — those persisted rows are the only place the internal
  `document_product_candidate_id` a job needs actually exists (the
  in-memory result only has the external string candidate id).
- **`SkuMappingHandler`** (`worker/handlers/sku_mapping.py`, new): loads the
  tenant-scoped `ProcessingJob` + `DocumentProductCandidate`, loads the
  completed analysis result, builds the existing `SkuMappingTask`, runs the
  existing retriever/tools/`ClaudeSkuMappingAgent` unchanged, persists via
  `SkuMappingPersistenceService`, reload-validates. Central design point,
  stated explicitly by the user and implemented as such: **`match`,
  `no_match`, and `ambiguous` are all business outcomes and all count as a
  completed job** — the handler returns normally for all three; only a
  genuine technical failure (source/candidate unavailable, retrieval
  missing, persistence/reload failure, unhandled exception) raises
  `JobExecutionError`. Registered in `worker/handlers/__init__.py` and
  `worker/__init__.py`'s dispatcher map, given its own
  `SkuMappingPersistenceService` instance alongside the existing
  `DocumentAnalysisPersistenceService`.
- **Tests**, two new files, both focused (never touched supplied/ground-truth
  data, no live Claude calls anywhere):
  - `test_sku_mapping_eligibility.py` (5, all passing): every
    `CommercialStatus` maps to its documented disposition; ambiguous is
    `review` specifically (not schedule, not skip); the policy's dict
    covers every enum member; version constant sanity check.
  - `test_sku_mapping_worker_integration.py` (11, all passing): schema
    constraint tests (link-required CHECK rejects partial fields and
    rejects fields on non-`sku_mapping` job types; composite FK rejects a
    candidate genuinely belonging to a different analysis run; partial
    unique index rejects a second concurrent active job for the same
    candidate); `schedule_sku_mapping_job` idempotency and
    retry-after-terminal-status; `DocumentAnalysisHandler` schedules a job
    only for the eligible (purchased) candidate out of three seeded
    (ambiguous/optional/purchased); `SkuMappingHandler` completes a MATCH
    decision, treats NO_MATCH and AMBIGUOUS as completions too
    (parametrized), and raises on a missing source — the last three use a
    `_FakeSkuMappingAgent` returning a pre-built `SkuMappingExecution`, no
    real agent/Claude involved.
  - Real bugs hit and fixed along the way, none in production code:
    (1) a Python cleanup pass I ran to strip redundant local imports left
    broken indentation in one parametrized test body — fixed by rewriting
    that test cleanly rather than patching the indentation; (2) four
    constraint tests were structured as
    `async with session.begin(): session.add(...)` followed by a separate
    `pytest.raises(IntegrityError): await session.commit()` — wrong,
    because `session.begin()`'s `__aexit__` already commits and raises the
    real error *before* the `pytest.raises` block is reached; fixed by
    removing the inner `session.begin()` wrapper so the add and the
    asserted-failing commit are in the same block; (3) the same
    duplicate-active-catalog-version collision fixed twice already in Run
    12 (`test_sku_mapping_persistence.py`) recurred here because the
    cross-lineage FK test calls `_seed()` twice under one organization —
    fixed with the identical `with_catalog: bool = True` escape-hatch
    pattern, `False` on the second, unrelated seed call.
- **Focused regression run** (sku_mapping suite + document_analysis +
  worker + api, 193 tests, all passing) confirms nothing in the existing
  pipeline regressed. Full suite was not run, per instruction.
- Added a dated `PROJECT_NOTES.md` decision-log entry covering the schema
  changes, composite FKs, partial unique index, eligibility policy, and the
  match/no_match/ambiguous-are-completions design point.
- Deferred, unchanged: Concern 2 (linking `GlobalTerm` facts to SKUs).
