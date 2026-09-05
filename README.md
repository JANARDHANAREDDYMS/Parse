# Maximor AI take-home

This backend connects a client-facing FastAPI service to PostgreSQL-backed
`processing_jobs` and a separate Python worker. It accepts PDFs and performs
deterministic structural inspection, native-text extraction, layout and table
extraction, page rendering, quality checks, and conditional OCR. SKU mapping and LLM
agent execution are not implemented yet.

The worker claims only job types with registered handlers. `pipeline_smoke_test` is
routed to storage-integrity verification, and `document_preprocessing` is routed to
the deterministic preprocessing engine. Normal PDF uploads create preprocessing jobs.

The organization UUID in API paths is temporary tenancy context. It is **not**
authentication or authorization.

## Local setup

Run commands from the project root:

```bash
cd /Users/janardhanareddyms/Documents/PROJECTS/maximor-ai-takehome
cp .env.example .env
python3 -m venv --prompt maximor-ai-takehome src/.venv
source src/.venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r src/requirements-dev.txt
```

Edit the development placeholders in `.env`, keeping it untracked. Activating a
virtual environment is a shell operation; after activation, `python` and `pip`
resolve to the environment's interpreter and installer. Confirm with
`python -c 'import sys; print(sys.executable)'`.

## PostgreSQL and migrations

```bash
docker compose up -d postgres
docker compose ps
PYTHONPATH=src src/.venv/bin/alembic -c alembic.ini upgrade head
PYTHONPATH=src src/.venv/bin/alembic -c alembic.ini current
PYTHONPATH=src src/.venv/bin/python -m maximor.db.health
```

## API and worker

Start the API in one terminal:

```bash
cd /Users/janardhanareddyms/Documents/PROJECTS/maximor-ai-takehome
PYTHONPATH=src src/.venv/bin/uvicorn maximor.api.app:app --host 127.0.0.1 --port 8000
```

Run the worker continuously in another terminal:

```bash
cd /Users/janardhanareddyms/Documents/PROJECTS/maximor-ai-takehome
PYTHONPATH=src src/.venv/bin/python -m maximor.worker
```

Or claim at most one job and exit:

```bash
PYTHONPATH=src src/.venv/bin/python -m maximor.worker --once
```

## Manual preprocessing test

Find the demo organization's UUID, then upload one PDF. Replace the two shell
placeholders below with returned identifiers; neither is a server path.

```bash
docker compose exec -T postgres psql -U maximor -d maximor -Atc "SELECT id FROM organizations WHERE slug = 'demo';"
curl --fail-with-body http://127.0.0.1:8000/health/live
curl --fail-with-body http://127.0.0.1:8000/health/ready
curl --fail-with-body -X POST -F 'file=@data/synthetic_order_form_dataset_50/pdfs/of-0001.pdf;type=application/pdf' http://127.0.0.1:8000/v1/organizations/ORGANIZATION_UUID/documents
PYTHONPATH=src src/.venv/bin/python -m maximor.worker --once
curl --fail-with-body http://127.0.0.1:8000/v1/organizations/ORGANIZATION_UUID/jobs/JOB_UUID
```

Uploaded objects are generated beneath the ignored `.runtime/documents`
directory. PostgreSQL stores only relative object keys. Validated preprocessing
results are compact JSON stored losslessly as `.json.gz` artifacts; page, block, and
table projections are stored relationally for targeted retrieval.

## Automated tests

The tests generate minimal PDF-signature byte fixtures and never access the
dataset, catalog, preview text, manifest, or ground truth.

```bash
PYTHONPATH=src src/.venv/bin/python -m pytest -q -c src/pytest.ini src/tests
```
