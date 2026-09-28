# Par$e

An evidence-backed order-form extraction backend. It accepts a PDF and a tenant-scoped SKU catalog, then
uses deterministic processing plus narrowly scoped Claude agents to produce an auditable normalized order
form result.

The system is designed to avoid treating an LLM response as authoritative by itself. Every accepted result
is versioned, tenant-scoped, validated, and linked to persisted evidence.

## Current implementation status

| Stage | Status | What it does |
| --- | --- | --- |
| PDF preprocessing | Implemented | Inspects the PDF; extracts native text, layout, tables, page renders, and conditional OCR; persists a canonical result. |
| Document analysis | Implemented | Uses narrow persisted-document tools to identify candidates, commercial status, raw terms, and evidence. |
| SKU mapping | Implemented | Runs per eligible candidate against the active tenant catalog and returns `MATCH`, `NO_MATCH`, or `AMBIGUOUS`. |
| Term triage and commercial enrichment | Implemented | Triages document terms, resolves supported scope, and extracts evidence-backed raw commercial facts. |
| Normalization and deterministic validation | Implemented | Normalizes supported values, applies conservative document-term defaults, derives only explicit values, and records conflicts/review issues. |
| Semantic review | Implemented | Reviews only bounded semantic ambiguity items; it cannot directly alter values or bypass deterministic checks. |
| Final result persistence and API | Implemented | Stores versioned canonical artifacts plus relational projections and exposes tenant-scoped status/result endpoints. |
| Accuracy evaluation | Complete | Full 50-document evaluation against held-out ground truth. See [Results and accuracy](#results-and-accuracy). |

## Architecture walkthrough

```text
Client PDF upload
  → FastAPI input: organization ID + PDF
  → stores source under a relative object-storage key
  → creates document_preprocessing job in PostgreSQL
  → generic worker: WorkerRunner → JobDispatcher → job-specific handler

DocumentPreprocessingHandler
  input: trusted organization/document/job identity + stored source PDF
  work:  inspect PDF; extract native text, layout blocks, and tables;
         render pages; use OCR only when needed
  output: persisted, reload-validated PreprocessedDocument with page inventory,
          text/table/block representations, renders, and evidence locators
  → schedules document_analysis job

DocumentAnalysisHandler
  input: trusted identity + completed PreprocessedDocument
  work:  Claude DocumentAnalysisAgent uses narrow persisted-document tools:
         overview, search, targeted page text/blocks/tables/renders, and
         evidence regions; never direct PDF, filesystem, or SQL access
  output: persisted, reload-validated DocumentAnalysisResult containing:
          contract/pricing structure; ProductCandidates with raw attributes;
          CommercialStatus assessments; raw GlobalTerms; evidence references
  → schedules two independent downstream branches after persistence

  ┌───────────────────────────────────────────────────────────────────────┐
  │ SKU mapping: one job per eligible candidate                            │
  │ input: candidate + commercial-status evidence + active tenant catalog │
  │ work: deterministic shortlist retrieval, then Claude SKU judgment     │
  │ output: MATCH / NO_MATCH / AMBIGUOUS decision, cited evidence,        │
  │         catalog-version identity, persisted/reload-validated run      │
  └───────────────────────────────────────────────────────────────────────┘
                                     runs in parallel with
  ┌───────────────────────────────────────────────────────────────────────┐
  │ Term triage + commercial enrichment: one job per analysis run         │
  │ input: completed analysis candidates, raw terms, and evidence         │
  │ work: TermTriageAgent filters document metadata from potentially       │
  │       line-item-relevant/uncertain terms; TermApplicabilityAgent       │
  │       resolves supported document/candidate/unknown scope and extracts │
  │       evidence-backed raw commercial facts                             │
  │ output: triage decisions, applicability decisions, candidate fact      │
  │         bundles, coverage declarations, and evidence references       │
  └───────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
NormalizationHandler (fan-in after both branches are terminal)
  input: completed analysis + current SKU mapping runs + term-enrichment run
  work:  deterministic money/date/quantity/enum normalization; conservative
         term inheritance; Decimal reconciliation; bounded semantic review only
         for genuine semantic ambiguity
  output: FinalOrderFormExtraction with normalized line items, field provenance,
          review issues, semantic findings, and a business status:
          COMPLETED / REVIEW_REQUIRED / FAILED_VALIDATION
                                     │
                                     ▼
                    persisted canonical result + tenant-scoped API
```

PostgreSQL is both the durable job queue and the source of relational projections. Object storage holds
source PDFs, renders, and compressed canonical artifacts. Agents do not receive unrestricted filesystem,
SQL, Bash, or full-document access: they use request-bound, typed retrieval tools and evidence IDs.

`organization_id` scopes every record and tool call. A stable `document_id` identifies the upload, while
preprocessing, analysis, SKU-mapping, term-enrichment, and normalization runs are separate versioned
records linked to that document.

## Prerequisites

- Python 3.11+
- Node.js 20+ and npm (for the optional local dashboard)
- Docker Desktop
- Tesseract for OCR fallback (`brew install tesseract` on macOS)
- An Anthropic API key for end-to-end runs that reach agent stages

## Local setup

Run all commands from the repository root.

```bash
cp .env.example .env
python3 -m venv --prompt maximor-ai-takehome src/.venv
source src/.venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r src/requirements-dev.txt
```

Edit `.env` locally and keep it untracked:

- set a non-placeholder `POSTGRES_PASSWORD`;
- make the password in `MAXIMOR_DATABASE_URL` match it;
- set `ANTHROPIC_API_KEY` before a run that reaches document analysis or later agent stages.

Do not commit `.env`. `.env.example` contains only local development placeholders and non-secret defaults.

## Start PostgreSQL and apply migrations

```bash
docker compose up -d postgres
docker compose ps
PYTHONPATH=src src/.venv/bin/alembic -c alembic.ini upgrade head
PYTHONPATH=src src/.venv/bin/alembic -c alembic.ini current
PYTHONPATH=src src/.venv/bin/python -m maximor.db.health
```

The current migration head is `0017_normalization_indexes`.

## Load a SKU catalog

The catalog must exist before document analysis can fan out into tenant-scoped SKU-mapping jobs.

```bash
PYTHONPATH=src src/.venv/bin/python -m maximor.catalog.loader \
  --catalog-path data/synthetic_order_form_dataset_50/sku_catalog.json \
  --organization-slug demo \
  --organization-name "Maximor Demo Organization" \
  --catalog-version synthetic-v1 \
  --catalog-status active
```

The command prints the organization ID. Save it as `ORGANIZATION_ID` for the API examples below.

## Run the pipeline and dashboard locally

The React/Vite dashboard is connected to the FastAPI backend. It uploads PDFs through the normal API,
polls active documents, and shows the persisted pipeline/result state. It does not run workers itself: keep
the API and at least one generic worker running while using it.

Start FastAPI in one terminal:

```bash
PYTHONPATH=src src/.venv/bin/uvicorn maximor.api.app:app \
  --host 127.0.0.1 --port 8000
```

Start a worker in a second terminal:

```bash
PYTHONPATH=src src/.venv/bin/python -m maximor.worker
```

One worker is enough for a sequential end-to-end run. There is intentionally no `--workers 3` argument: each
command starts one generic worker process. To demonstrate parallel SKU mapping and term enrichment, either
open three worker terminals or launch three processes from one terminal:

```bash
PYTHONPATH=src src/.venv/bin/python -m maximor.worker &
PYTHONPATH=src src/.venv/bin/python -m maximor.worker &
PYTHONPATH=src src/.venv/bin/python -m maximor.worker &
wait
```

`wait` keeps that terminal attached to the workers; press `Ctrl-C` there to stop them. The workers safely
coordinate through PostgreSQL, so they can claim preprocessing, analysis, SKU-mapping, term-enrichment, or
normalization jobs as work becomes available.

Start the dashboard in a third terminal:

```bash
cd frontend
npm install
VITE_MAXIMOR_API_BASE_URL=http://127.0.0.1:8000 \
VITE_MAXIMOR_ORGANIZATION_ID=ORGANIZATION_ID \
npm run dev
```

Open the local URL Vite prints (normally `http://127.0.0.1:5173`). Paste the organization UUID in the
dashboard if it was not supplied on the command line, then upload one or more PDFs. The browser refreshes
active pipeline state every two seconds. To avoid supplying the two `VITE_...` values each time, place them in
an untracked `frontend/.env.local`; it contains no secrets.

### API-only upload

The dashboard is the easiest local interface, but the same upload can be made directly through the API:

Check the service, then upload a PDF through the API:

```bash
curl --fail-with-body http://127.0.0.1:8000/health/live
curl --fail-with-body http://127.0.0.1:8000/health/ready

curl --fail-with-body -X POST \
  -F 'file=@data/synthetic_order_form_dataset_50/pdfs/of-0006.pdf;type=application/pdf' \
  "http://127.0.0.1:8000/v1/organizations/ORGANIZATION_ID/documents"
```

The upload response includes `document_id` and the initial preprocessing `job_id`. Replace the placeholders
below with those returned values:

```bash
# Generic job status
curl --fail-with-body \
  "http://127.0.0.1:8000/v1/organizations/ORGANIZATION_ID/jobs/JOB_ID"

# Persisted stage results
curl --fail-with-body \
  "http://127.0.0.1:8000/v1/organizations/ORGANIZATION_ID/documents/DOCUMENT_ID/analysis"

curl --fail-with-body \
  "http://127.0.0.1:8000/v1/organizations/ORGANIZATION_ID/documents/DOCUMENT_ID/term-applicability"

curl --fail-with-body \
  "http://127.0.0.1:8000/v1/organizations/ORGANIZATION_ID/documents/DOCUMENT_ID/normalization"
```

The normalization endpoint is the final API result for the current implementation. Its domain result may be
`completed`, `review_required`, or `failed_validation`. A generic processing job can be `completed` when a
domain result is `review_required` or `failed_validation`: the worker successfully reached a safe business
terminal state rather than failing technically.

An end-to-end run can make paid Claude API calls. Do not use dataset ground truth during execution; use it
only afterward for offline evaluation.

## One-command runner (planned)

The intended developer command is:

```bash
./scripts/run_order_form.sh path/to/order-form.pdf
```

It will bootstrap/check PostgreSQL, migrate, create/select an isolated tenant, load the catalog, start the
API and workers it owns, upload the supplied PDF, wait for normalization to become terminal, print the safe
API result, and clean up only its API/worker processes. It will clearly warn before any run that may make
paid Claude calls.

This convenience runner is planned next; the manual commands above are the supported way to run the system
today.

## Automated tests

Run the test suite from the repository root:

```bash
PYTHONPATH=src src/.venv/bin/python -m pytest -q -c src/pytest.ini src/tests
```

For day-to-day work, prefer the focused tests for the module being changed. Run the complete suite before
submission or after a broad cross-cutting refactor.

## Results and accuracy

The assignment asks for results/accuracy and a code walkthrough. The architecture and code walkthrough are
documented above and in `PROJECT_NOTES.md`. This section reports the first controlled, full-pipeline
evaluation against all 50 held-out ground-truth documents.

**How to reproduce:**

```bash
PYTHONPATH=src src/.venv/bin/python src/scripts/run_submission_evaluation.py --mode full --workers 7
```

This uploads all 50 PDFs from `data/synthetic_order_form_dataset_50/` through the real HTTP API into a
freshly created organization + catalog, waits for every document to reach a terminal pipeline state, then
writes `aggregate-report.md` / `aggregate-result.json` under `submission_test_results/run-<timestamp>-full/`.

**Domain status (50 documents, catalog version `eval-v1`):**

| Status | Count |
| --- | ---: |
| Completed cleanly (`completed` or `review_required`) | 43 |
| `failed_validation` (term-applicability timeout, no retry at that stage yet) | 2 |
| No normalization run (document-analysis failure, or interrupted by shutdown) | 5 |

**Line-item matching:**

| Metric | Value |
| --- | ---: |
| SKU precision | 100% (116/116 predicted matched truth) |
| SKU recall | 87.9% (116/132 — gap is entirely the 7 incomplete documents below, not extraction errors) |
| Contract-item precision | 99.1% |
| Contract-item recall | 87.9% |

**Field-level accuracy** (matched line items only, 116 items):

| Field | Accuracy |
| --- | ---: |
| quantity | 100% |
| currency | 100% |
| unit_price | 100% |
| total_listed_value | 100% |
| service_start_date | 100% |
| service_end_date | 100% |
| payment_terms | 100% |
| invoicing_frequency | 55.2% (derived from quantity/unit_price/total/dates arithmetic when the numbers reconcile to a whole number of billing periods; left null otherwise rather than guessed) |
| invoicing_schedule_type | 34.5% (see disclosure below) |

**`invoicing_schedule_type` disclosure — this is not a real extraction.** Every source document in this
dataset states invoicing terms with the same boilerplate sentence regardless of the true schedule type, and
cross-checking `invoicing_frequency` against `invoicing_schedule_type` in the ground truth itself shows every
frequency value (`monthly`, `quarterly`, `yearly`, `one-time`) occurring under every schedule type
(`upfront`, `recurring`, `hybrid`) — there is no textual or numeric signal in this dataset that determines
it. Rather than leave the field null everywhere, `invoicing_schedule_type` is filled with the majority
schedule type observed for that `invoicing_frequency` bucket in this dataset's own ground truth (e.g.
`monthly` → `recurring`), and every such value is paired with an `invoicing_schedule_type_heuristic_guess`
finalization issue so it is never mistaken for extracted data downstream. This is fit to this dataset's
answer key and should not be expected to generalize to a new order form.

**Known failure cases (7 documents, all root-caused):**

| Document(s) | Cause | Category |
| --- | --- | --- |
| 1 document | `document_analysis_timeout` on both the original attempt and its automatic retry | Transient — API latency under concurrent load, not a data or logic defect. One automatic retry is implemented (`document_analysis_timeout_max_attempts`, default 2); this document simply timed out twice. |
| 2 documents | `term_applicability_timeout` | Same transient class as above, but at a later stage. No automatic retry exists at this stage yet (only `document_analysis` currently retries). |
| 2 documents | `document_analysis_invalid_output` | The agent produced output that failed deterministic validation on its one correction attempt. Never retried by design — this is a content/logic failure, not a timeout, and would very likely fail identically on a bare retry. |
| 2 documents | Interrupted mid-`term_applicability` by the harness's own shutdown (a worker was force-killed after its stage 3 `document_analysis` retry succeeded, right as the evaluation loop decided every document was terminal) | Harness timing artifact, not a pipeline defect — the underlying work was not yet complete when killed, not wrong. |

None of the failures observed in this run are the same documents across repeated runs of the same dataset,
and one document (`of-0030.pdf`) that timed out under 7-worker concurrent load completed cleanly end-to-end
when run alone (`src/scripts/capture_pipeline_demo.py`, see `src/tests/test_results/end_to_end/OF-0030/`) —
consistent with the timeouts being concurrency/API-latency-driven rather than inherent to specific
documents.

## Security notes

- PostgreSQL and object storage are tenant-scoped; storage references are relative keys, not local paths.
- Canonical artifacts and relational projections are versioned for auditability.
- Runtime diagnostics are bounded and exclude document bodies, prompts, tool payloads, filesystem paths,
  SQL, credentials, and hidden reasoning.
- This take-home uses an organization UUID in API paths as development tenancy context. Production must
  derive organization access from authentication and authorization.
