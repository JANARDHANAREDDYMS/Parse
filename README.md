# Maximor AI take-home

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
| Accuracy evaluation | Pending | Will be added after the first controlled full-pipeline evaluation against held-out ground truth. No accuracy figure is claimed yet. |

## Architecture walkthrough

```text
PDF upload
  → FastAPI stores the source under a relative object-storage key
  → PostgreSQL processing_jobs queue
  → generic worker: WorkerRunner → JobDispatcher → handler
  → document preprocessing
  → document analysis
  → SKU mapping jobs (one per eligible candidate) ─┐
  → term triage + commercial enrichment ───────────┤ run in parallel
                                                    ▼
                                           normalization/finalization
                                                    ▼
                                   COMPLETED / REVIEW_REQUIRED / FAILED_VALIDATION
                                                    ▼
                                      persisted result + tenant-scoped API
```

PostgreSQL is both the durable job queue and the source of relational projections. Object storage holds
source PDFs, renders, and compressed canonical artifacts. Agents do not receive unrestricted filesystem,
SQL, Bash, or full-document access: they use request-bound, typed retrieval tools and evidence IDs.

`organization_id` scopes every record and tool call. A stable `document_id` identifies the upload, while
preprocessing, analysis, SKU-mapping, term-enrichment, and normalization runs are separate versioned
records linked to that document.

## Prerequisites

- Python 3.11+
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

## Run the pipeline manually

Start FastAPI in one terminal:

```bash
PYTHONPATH=src src/.venv/bin/uvicorn maximor.api.app:app \
  --host 127.0.0.1 --port 8000
```

Start one or more generic workers in other terminals. Three workers demonstrate the intended parallel
SKU-mapping and term-enrichment fan-out:

```bash
PYTHONPATH=src src/.venv/bin/python -m maximor.worker
```

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
documented above and in `PROJECT_NOTES.md`. A final accuracy table is intentionally pending the first
controlled full-pipeline evaluation: it will report the evaluated PDF set, catalog version, final domain
statuses, line-item SKU precision/recall, field-level accuracy, unresolved/review-required counts, and
known failure cases. No result or accuracy figure should be claimed before that evaluation is reproducible.

## Security notes

- PostgreSQL and object storage are tenant-scoped; storage references are relative keys, not local paths.
- Canonical artifacts and relational projections are versioned for auditability.
- Runtime diagnostics are bounded and exclude document bodies, prompts, tool payloads, filesystem paths,
  SQL, credentials, and hidden reasoning.
- This take-home uses an organization UUID in API paths as development tenancy context. Production must
  derive organization access from authentication and authorization.
