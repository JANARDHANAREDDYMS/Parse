"""One-shot harness: fresh org+catalog, upload PDFs, monitor to terminal, reload+evaluate.

Not part of the pytest suite -- this makes real paid Claude calls once workers
start processing uploaded documents. Run manually:

    PYTHONPATH=src src/.venv/bin/python src/scripts/run_submission_evaluation.py \\
        --mode pilot --workers 5

    PYTHONPATH=src src/.venv/bin/python src/scripts/run_submission_evaluation.py \\
        --mode full --workers 6

Phases (in order, each run exactly once):
  1. Preflight: PostgreSQL health, alembic-at-head, no stray API/worker
     processes, optional focused pytest subset.
  2. Setup: fresh isolated organization + the 21-SKU catalog loaded once via
     the existing `maximor.catalog.loader`.
  3. Start API + N uniquely-identified worker subprocesses (this script owns
     them and stops only these at the end).
  4. Upload the selected PDFs once through the real HTTP endpoint.
  5. Monitor `processing_jobs`/`normalization_runs` read-only until every
     uploaded document is terminal, printing + persisting a compact status
     line every 60 seconds. Never retries a document automatically.
  6. Once every document is terminal: reload every completed stage through
     the existing persistence services (no PDF reopened, no manual agent
     invocation), then -- and only then -- read ground truth for offline
     evaluation and write the aggregate report.
  7. Shut down only the API/worker processes this run started. PostgreSQL
     and every persisted row are left intact.

Output goes to submission_test_results/run-<UTC timestamp>-<mode>/ at the
repo root (gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import signal
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
VENV_BIN = SRC_ROOT / ".venv" / "bin"
DATASET_ROOT = REPO_ROOT / "data" / "synthetic_order_form_dataset_50"
MANIFEST_PATH = DATASET_ROOT / "manifest.jsonl"
CATALOG_PATH = DATASET_ROOT / "sku_catalog.json"
RESULTS_ROOT = REPO_ROOT / "submission_test_results"

API_HOST = "127.0.0.1"
API_PORT = 8000
API_BASE = f"http://{API_HOST}:{API_PORT}"

MAX_WORKERS_WITHOUT_OVERRIDE = 8
STATUS_INTERVAL_SECONDS = 60
POLL_INTERVAL_SECONDS = 5
DEFAULT_MAX_WAIT_MINUTES = 180

TERMINAL_JOB_STATUSES = {"completed", "failed"}
NORMALIZATION_DOMAIN_TERMINAL_STATUSES = {"completed", "review_required", "failed_validation"}

# Focused, fast, already-established regression coverage for the exact code
# paths this harness depends on (worker handlers, fan-in scheduling,
# persistence, API). Not a substitute for the full suite -- a last sanity
# check before spending money. Override with --preflight-tests / --skip-preflight-tests.
DEFAULT_PREFLIGHT_TESTS = (
    "tests/test_normalization_stage5c.py",
    "tests/test_normalization_stage5c_scheduling.py",
    "tests/test_normalization_stage5c_persistence.py",
    "tests/test_normalization_stage5c_worker.py",
    "tests/test_normalization_stage5c_api.py",
    "tests/test_worker_handlers_fanin_on_failure.py",
    "tests/test_term_applicability_worker_integration.py",
    "tests/test_term_applicability_failure_diagnostics.py",
    "tests/test_sku_mapping_worker_integration.py",
    "tests/test_api.py",
)

FIELD_COMPARISONS = (
    "quantity", "currency", "unit_price", "total_listed_value",
    "service_start_date", "service_end_date",
    "invoicing_schedule_type", "invoicing_frequency", "payment_terms",
)


# --- manifest / PDF selection --------------------------------------------------------

@dataclass(frozen=True)
class ManifestRecord:
    form_id: str
    quote_number: str
    contract_item_count: int
    slug: str
    pdf_path: Path
    ground_truth_path: Path


def load_manifest() -> list[ManifestRecord]:
    """Load all 50 dataset records, resolving paths locally (manifest paths are stale)."""

    records = []
    with MANIFEST_PATH.open() as fh:
        for line in fh:
            raw = json.loads(line)
            slug = raw["form_id"].lower()
            records.append(ManifestRecord(
                form_id=raw["form_id"], quote_number=raw["quote_number"],
                contract_item_count=raw["contract_item_count"], slug=slug,
                pdf_path=DATASET_ROOT / "pdfs" / f"{slug}.pdf",
                ground_truth_path=DATASET_ROOT / "ground_truth" / f"{slug}.json",
            ))
    return records


def select_pilot_pdfs(records: list[ManifestRecord], *, count: int = 5) -> list[ManifestRecord]:
    """A fresh random sample of `count` PDFs from the full dataset each run.

    Not seeded -- each invocation draws a different sample so repeated pilots
    exercise different documents instead of always re-checking the same five.
    """

    return random.sample(records, min(count, len(records)))


# --- preflight -----------------------------------------------------------------------

def check_postgres_healthy() -> None:
    result = subprocess.run(["pg_isready"], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"[preflight] PostgreSQL is not accepting connections:\n{result.stdout}{result.stderr}")
    print(f"[preflight] {result.stdout.strip()}")


def check_alembic_head() -> None:
    result = subprocess.run(
        [str(VENV_BIN / "alembic"), "current"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
    )
    output = result.stdout + result.stderr
    if "(head)" not in output:
        raise SystemExit(f"[preflight] Database is not at the alembic head revision:\n{output}")
    print(f"[preflight] alembic at head: {output.strip().splitlines()[-1]}")


def check_no_stray_processes(*, allow_existing: bool = False) -> None:
    for pattern in ("uvicorn maximor.api.app", "maximor.worker"):
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            if allow_existing:
                print(f"[preflight] ignoring pre-existing process matching {pattern!r} (PIDs: {result.stdout.strip()}) per --allow-existing-processes")
                continue
            raise SystemExit(f"[preflight] Stray process matching {pattern!r} already running (PIDs: {result.stdout.strip()})")
    print("[preflight] no stray API/worker processes to worry about")


def run_preflight_tests(test_targets: tuple[str, ...]) -> None:
    if not test_targets:
        print("[preflight] skipping automated tests (--skip-preflight-tests)")
        return
    print(f"[preflight] running {len(test_targets)} focused test files...")
    result = subprocess.run(
        [str(VENV_BIN / "python"), "-m", "pytest", *test_targets, "-q"],
        cwd=str(SRC_ROOT), capture_output=True, text=True,
    )
    tail = "\n".join(result.stdout.strip().splitlines()[-15:])
    print(f"[preflight] {tail}")
    if result.returncode != 0:
        raise SystemExit("[preflight] focused tests failed -- aborting before spending anything.")


# --- process management ---------------------------------------------------------------

@dataclass
class ManagedProcess:
    name: str
    popen: subprocess.Popen
    log_path: Path
    log_file: Any


def start_api(log_dir: Path) -> ManagedProcess:
    log_path = log_dir / "api.log"
    log_file = log_path.open("w")
    popen = subprocess.Popen(
        [str(VENV_BIN / "uvicorn"), "maximor.api.app:app", "--host", API_HOST, "--port", str(API_PORT), "--app-dir", str(SRC_ROOT)],
        cwd=str(REPO_ROOT), env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
        stdout=log_file, stderr=subprocess.STDOUT,
    )
    return ManagedProcess("api", popen, log_path, log_file)


def wait_for_api_health(timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{API_BASE}/health/ready", timeout=2.0)
            if response.status_code == 200:
                print("[setup] API is ready")
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise SystemExit("[setup] API did not become ready in time")


def start_workers(count: int, log_dir: Path) -> list[ManagedProcess]:
    processes = []
    for i in range(1, count + 1):
        log_path = log_dir / f"worker-{i}.log"
        log_file = log_path.open("w")
        env = {**os.environ, "PYTHONPATH": str(SRC_ROOT), "MAXIMOR_WORKER_IDENTITY": f"eval-worker-{i}"}
        popen = subprocess.Popen(
            [str(VENV_BIN / "python"), "-m", "maximor.worker"],
            cwd=str(REPO_ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT,
        )
        processes.append(ManagedProcess(f"worker-{i}", popen, log_path, log_file))
    print(f"[setup] started {count} workers")
    return processes


def shutdown_processes(processes: list[ManagedProcess], timeout_seconds: float = 10.0) -> None:
    for proc in processes:
        try:
            proc.popen.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout_seconds
    for proc in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.popen.wait(timeout=remaining)
            print(f"[shutdown] {proc.name} exited cleanly")
        except subprocess.TimeoutExpired:
            proc.popen.kill()
            proc.popen.wait()
            print(f"[shutdown] {proc.name} killed after timeout")
        finally:
            proc.log_file.close()


# --- upload --------------------------------------------------------------------------

async def upload_documents(organization_id: str, records: list[ManifestRecord], manifest_out: dict, manifest_path: Path) -> dict[str, dict]:
    """POST each PDF once through the real endpoint; return per-form upload metadata.

    Writes `manifest.json` after every single upload, not just at the end:
    a worker starts a real job (and can incur real cost) the moment a job
    row exists, so a transient failure partway through a 50-document upload
    loop must never leave already-uploaded documents with no local record of
    their document_id/job_id.
    """

    uploaded: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=60.0) as client:
        for record in records:
            with record.pdf_path.open("rb") as fh:
                response = await client.post(
                    f"{API_BASE}/v1/organizations/{organization_id}/documents",
                    files={"file": (record.pdf_path.name, fh, "application/pdf")},
                )
            response.raise_for_status()
            body = response.json()
            uploaded[record.form_id] = {
                "document_id": body["document_id"], "job_id": body["job_id"],
                "quote_number": record.quote_number, "contract_item_count": record.contract_item_count,
                "slug": record.slug,
            }
            print(f"[upload] {record.form_id} -> document {body['document_id']}")
            manifest_out["uploaded"] = uploaded
            manifest_path.write_text(json.dumps(manifest_out, indent=2, sort_keys=True, default=str))
    return uploaded


# --- monitoring ------------------------------------------------------------------------

def _fmt_elapsed(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


async def snapshot(document_ids: list[uuid.UUID]) -> dict:
    """Read-only snapshot of every job/normalization row for the uploaded documents."""

    from maximor.db.models import DocumentAnalysisRun, NormalizationRun, ProcessingJob, SkuMappingRun
    from maximor.db.session import get_session_factory

    sessions = get_session_factory()
    async with sessions() as session:
        jobs = (await session.scalars(select(ProcessingJob).where(ProcessingJob.document_id.in_(document_ids)))).all()
        norm_runs = (await session.scalars(select(NormalizationRun).where(NormalizationRun.document_id.in_(document_ids)))).all()
        analysis_runs = (await session.scalars(select(DocumentAnalysisRun).where(DocumentAnalysisRun.document_id.in_(document_ids)))).all()
        sku_runs = (await session.scalars(select(SkuMappingRun).where(SkuMappingRun.document_id.in_(document_ids)))).all()
    return {"jobs": jobs, "norm_runs": norm_runs, "analysis_runs": analysis_runs, "sku_runs": sku_runs}


def _document_terminal(document_id: uuid.UUID, jobs_by_doc: dict) -> bool:
    """A document is terminal once no further job can possibly be scheduled for it.

    Preprocessing/analysis failing stops the chain outright (no job schedules
    the next stage on failure). Otherwise, analysis succeeding always
    schedules term_applicability, and the fan-in-on-failure fix guarantees
    normalization eventually gets scheduled once sku_mapping/term_applicability
    are both terminal (success or failure) -- so "a normalization job exists
    and reached a terminal processing status" is a complete terminal signal.
    """

    jobs = jobs_by_doc.get(document_id, [])
    by_type: dict[str, list] = {}
    for job in jobs:
        by_type.setdefault(job.job_type, []).append(job)
    preprocessing = by_type.get("document_preprocessing", [])
    if preprocessing and preprocessing[0].status == "failed":
        return True
    analysis = by_type.get("document_analysis", [])
    if analysis and analysis[0].status == "failed":
        return True
    normalization = by_type.get("normalization", [])
    if normalization and normalization[-1].status in TERMINAL_JOB_STATUSES:
        return True
    return False


def build_status(uploaded: dict, snap: dict, started_at: float) -> dict:
    jobs = snap["jobs"]
    norm_runs = snap["norm_runs"]
    document_ids = {uuid.UUID(v["document_id"]) for v in uploaded.values()}
    jobs_by_doc: dict[uuid.UUID, list] = {}
    for job in jobs:
        jobs_by_doc.setdefault(job.document_id, []).append(job)

    def count(job_type: str, status: str | None = None) -> int:
        matches = [j for j in jobs if j.job_type == job_type]
        if status is not None:
            matches = [j for j in matches if j.status == status]
        return len(matches)

    normalization_domain_counts = Counter(run.status for run in norm_runs)
    terminal_documents = sum(1 for doc_id in document_ids if _document_terminal(doc_id, jobs_by_doc))

    cost_total = Decimal("0")
    cost_note = "term_applicability cost is only visible in runtime_diagnostics when a run fails/times out (no dedicated column on success) -- this total is a lower bound."
    for run in snap["analysis_runs"]:
        if run.reported_cost_usd:
            cost_total += run.reported_cost_usd
    for run in snap["sku_runs"]:
        if run.reported_cost_usd:
            cost_total += run.reported_cost_usd
    for run in norm_runs:
        if run.reported_cost_usd:
            cost_total += run.reported_cost_usd

    return {
        "elapsed_seconds": time.monotonic() - started_at,
        "uploaded": len(uploaded),
        "total_documents": len(document_ids),
        "preprocessing_complete": count("document_preprocessing", "completed"),
        "analysis_complete": count("document_analysis", "completed"),
        "analysis_failed": count("document_analysis", "failed"),
        "sku_jobs_complete": count("sku_mapping", "completed"),
        "sku_jobs_total": count("sku_mapping"),
        "enrichment_complete": count("term_applicability", "completed"),
        "enrichment_failed": count("term_applicability", "failed"),
        "normalization_completed": normalization_domain_counts.get("completed", 0),
        "normalization_review_required": normalization_domain_counts.get("review_required", 0),
        "normalization_failed_validation": normalization_domain_counts.get("failed_validation", 0),
        "normalization_failed": normalization_domain_counts.get("failed", 0),
        "terminal_documents": terminal_documents,
        "cost_so_far_usd": str(cost_total),
        "cost_note": cost_note,
    }


def format_status_line(status: dict) -> str:
    return (
        f"{_fmt_elapsed(status['elapsed_seconds'])} elapsed · "
        f"uploaded {status['uploaded']}/{status['total_documents']} · "
        f"preprocessing {status['preprocessing_complete']} complete · "
        f"analysis {status['analysis_complete']} complete / {status['analysis_failed']} failed · "
        f"SKU jobs {status['sku_jobs_complete']}/{status['sku_jobs_total']} complete · "
        f"enrichment {status['enrichment_complete']} complete / {status['enrichment_failed']} failed · "
        f"normalization {status['normalization_completed']} completed / "
        f"{status['normalization_review_required']} review-required / "
        f"{status['normalization_failed_validation']} failed-validation · "
        f"terminal documents {status['terminal_documents']}/{status['total_documents']} · "
        f"cost so far ${status['cost_so_far_usd']}"
    )


async def monitor_until_terminal(uploaded: dict, run_dir: Path, max_wait_minutes: float) -> None:
    document_ids = [uuid.UUID(v["document_id"]) for v in uploaded.values()]
    started_at = time.monotonic()
    deadline = started_at + max_wait_minutes * 60
    last_status_print = 0.0
    progress_log = (run_dir / "progress.log").open("a")
    try:
        while True:
            snap = await snapshot(document_ids)
            status = build_status(uploaded, snap, started_at)
            now = time.monotonic()
            if now - last_status_print >= STATUS_INTERVAL_SECONDS or last_status_print == 0.0:
                line = format_status_line(status)
                print(line)
                progress_log.write(f"{datetime.now(UTC).isoformat()} {line}\n")
                progress_log.flush()
                (run_dir / "progress.json").write_text(json.dumps(status, indent=2, sort_keys=True, default=str))
                last_status_print = now
            if status["terminal_documents"] >= status["total_documents"]:
                print("[monitor] every document is terminal")
                break
            if now >= deadline:
                print(f"[monitor] max wait of {max_wait_minutes} minutes reached with "
                      f"{status['terminal_documents']}/{status['total_documents']} terminal -- stopping monitor, "
                      "NOT retrying anything.")
                break
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        progress_log.close()


# --- per-document reload (no PDF, no manual agent invocation) --------------------------

async def reload_document(organization_id: uuid.UUID, document_id: uuid.UUID, form_id: str, out_dir: Path) -> dict:
    from maximor.config import get_database_settings
    from maximor.db.models import ProcessingJob
    from maximor.db.session import get_session_factory
    from maximor.normalization.persistence import NormalizationPersistenceService
    from maximor.storage import LocalObjectStorage

    settings = get_database_settings()
    sessions = get_session_factory()
    storage = LocalObjectStorage(settings.local_storage_root)

    async with sessions() as session:
        jobs = (await session.scalars(
            select(ProcessingJob).where(ProcessingJob.document_id == document_id).order_by(ProcessingJob.created_at)
        )).all()

    summary = {
        "form_id": form_id, "document_id": str(document_id),
        "jobs": [
            {
                "job_type": j.job_type, "status": j.status, "attempt_number": j.attempt_number,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "completed_at": j.completed_at.isoformat() if j.completed_at else None,
                "error_code": j.error_code,
            }
            for j in jobs
        ],
    }

    normalization_service = NormalizationPersistenceService(sessions, storage)
    norm_run = await normalization_service.latest_run(organization_id=organization_id, document_id=document_id)
    normalization_result = None
    if norm_run is not None and norm_run.status in NORMALIZATION_DOMAIN_TERMINAL_STATUSES:
        result = await normalization_service.load_completed_result(organization_id=organization_id, run_id=norm_run.id)
        normalization_result = json.loads(result.model_dump_json())
        summary["normalization_domain_status"] = result.status.value
    elif norm_run is not None:
        summary["normalization_domain_status"] = norm_run.status
        summary["normalization_error_code"] = norm_run.error_code
    else:
        summary["normalization_domain_status"] = None

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pipeline-summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str))
    if normalization_result is not None:
        (out_dir / "normalization-result.json").write_text(json.dumps(normalization_result, indent=2, sort_keys=True, default=str))
    return summary


# --- offline evaluation (ground truth read only after every document is terminal) -----

def _money_amount(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def compare_document(normalization_result: dict | None, ground_truth: dict) -> dict:
    predicted_items = (normalization_result or {}).get("extraction", {}).get("line_items", []) or []
    truth_items = ground_truth.get("contract_items", [])

    predicted_by_sku: dict[str, dict] = {item.get("sku_code"): item for item in predicted_items if item.get("sku_code")}
    truth_by_sku: dict[str, dict] = {item.get("sku_code"): item for item in truth_items if item.get("sku_code")}
    predicted_counts = Counter(item.get("sku_code") for item in predicted_items if item.get("sku_code"))
    truth_counts = Counter(item.get("sku_code") for item in truth_items if item.get("sku_code"))

    matched_skus = set(predicted_by_sku) & set(truth_by_sku)
    item_matched = sum(min(predicted_counts[sku], truth_counts[sku]) for sku in truth_counts)

    field_results = {field_name: {"correct": 0, "total": 0} for field_name in FIELD_COMPARISONS}
    for sku in matched_skus:
        predicted, truth = predicted_by_sku[sku], truth_by_sku[sku]
        for field_name in FIELD_COMPARISONS:
            truth_value = truth.get(field_name)
            if truth_value is None:
                continue
            field_results[field_name]["total"] += 1
            predicted_value = predicted.get(field_name)
            if field_name in ("unit_price", "total_listed_value"):
                predicted_amount = _money_amount(predicted_value)
                matches = predicted_amount is not None and abs(predicted_amount - float(truth_value)) < 0.01
            elif field_name == "quantity":
                matches = predicted_value is not None and abs(float(predicted_value) - float(truth_value)) < 1e-6
            else:
                matches = predicted_value == truth_value
            if matches:
                field_results[field_name]["correct"] += 1

    return {
        "sku_matched": len(matched_skus),
        "sku_predicted_only": len(set(predicted_by_sku) - set(truth_by_sku)),
        "sku_truth_only": len(set(truth_by_sku) - set(predicted_by_sku)),
        "predicted_sku_count": len(predicted_by_sku), "truth_sku_count": len(truth_by_sku),
        "item_matched": item_matched,
        "item_predicted_total": sum(predicted_counts.values()),
        "item_truth_total": sum(truth_counts.values()),
        "field_results": field_results,
    }


def _precision_recall(matched: int, predicted_total: int, truth_total: int) -> dict:
    precision = matched / predicted_total if predicted_total else None
    recall = matched / truth_total if truth_total else None
    return {"precision": precision, "recall": recall, "matched": matched, "predicted_total": predicted_total, "truth_total": truth_total}


def run_offline_evaluation(records_by_form: dict[str, ManifestRecord], documents_dir: Path) -> dict:
    per_document: dict[str, dict] = {}
    status_counts: Counter = Counter()
    failure_categories: Counter = Counter()
    failure_examples: dict[str, list[str]] = {}
    sku_matched = sku_predicted_total = sku_truth_total = 0
    item_matched = item_predicted_total = item_truth_total = 0
    field_totals = {field_name: {"correct": 0, "total": 0} for field_name in FIELD_COMPARISONS}
    versions_seen: dict[str, set] = {}

    for form_id, record in records_by_form.items():
        doc_dir = documents_dir / record.slug
        pipeline_summary_path = doc_dir / "pipeline-summary.json"
        if not pipeline_summary_path.exists():
            continue
        pipeline_summary = json.loads(pipeline_summary_path.read_text())
        domain_status = pipeline_summary.get("normalization_domain_status")
        status_counts[domain_status or "no_normalization_run"] += 1

        for job in pipeline_summary["jobs"]:
            if job["status"] == "failed" and job["error_code"]:
                failure_categories[job["error_code"]] += 1
                failure_examples.setdefault(job["error_code"], [])
                if len(failure_examples[job["error_code"]]) < 3:
                    failure_examples[job["error_code"]].append(form_id)

        normalization_result_path = doc_dir / "normalization-result.json"
        normalization_result = json.loads(normalization_result_path.read_text()) if normalization_result_path.exists() else None
        if normalization_result is not None:
            for field_name in ("schema_version", "finalization_policy_version"):
                versions_seen.setdefault(field_name, set()).add(normalization_result.get(field_name))

        ground_truth = json.loads(record.ground_truth_path.read_text())
        comparison = compare_document(normalization_result, ground_truth)
        per_document[form_id] = {"domain_status": domain_status, **comparison}

        sku_matched += comparison["sku_matched"]
        sku_predicted_total += comparison["predicted_sku_count"]
        sku_truth_total += comparison["truth_sku_count"]
        item_matched += comparison["item_matched"]
        item_predicted_total += comparison["item_predicted_total"]
        item_truth_total += comparison["item_truth_total"]
        for field_name, counts in comparison["field_results"].items():
            field_totals[field_name]["correct"] += counts["correct"]
            field_totals[field_name]["total"] += counts["total"]

        offline_eval_path = doc_dir / "offline-evaluation.json"
        offline_eval_path.write_text(json.dumps(per_document[form_id], indent=2, sort_keys=True, default=str))

    field_accuracy = {
        field_name: (counts["correct"] / counts["total"] if counts["total"] else None)
        for field_name, counts in field_totals.items()
    }

    return {
        "per_document": per_document,
        "status_counts": dict(status_counts),
        "failure_categories": dict(failure_categories),
        "failure_examples": failure_examples,
        "sku_precision_recall": _precision_recall(sku_matched, sku_predicted_total, sku_truth_total),
        "contract_item_precision_recall": _precision_recall(item_matched, item_predicted_total, item_truth_total),
        "field_accuracy": field_accuracy,
        "field_totals": field_totals,
        "versions_seen": {key: sorted(v for v in values if v is not None) for key, values in versions_seen.items()},
        "documents_evaluated": len(per_document),
    }


def write_aggregate_report(aggregate: dict, run_dir: Path) -> None:
    (run_dir / "aggregate-result.json").write_text(json.dumps(aggregate, indent=2, sort_keys=True, default=str))

    lines = ["# Submission evaluation report", ""]
    lines.append(f"Documents evaluated: {aggregate['documents_evaluated']}")
    lines.append("")
    lines.append("## Domain status counts")
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("| --- | ---: |")
    for status, count in sorted(aggregate["status_counts"].items()):
        lines.append(f"| {status} | {count} |")
    lines.append("")
    lines.append("## SKU precision/recall")
    spr = aggregate["sku_precision_recall"]
    lines.append(f"- precision: {spr['precision']}, recall: {spr['recall']} ({spr['matched']} matched / {spr['predicted_total']} predicted / {spr['truth_total']} truth)")
    lines.append("")
    lines.append("## Contract-item precision/recall")
    ipr = aggregate["contract_item_precision_recall"]
    lines.append(f"- precision: {ipr['precision']}, recall: {ipr['recall']} ({ipr['matched']} matched / {ipr['predicted_total']} predicted / {ipr['truth_total']} truth)")
    lines.append("")
    lines.append("## Field-level accuracy (matched line items only)")
    lines.append("")
    lines.append("| Field | Accuracy | Correct / Total |")
    lines.append("| --- | ---: | ---: |")
    for field_name, accuracy in aggregate["field_accuracy"].items():
        totals = aggregate["field_totals"][field_name]
        lines.append(f"| {field_name} | {accuracy} | {totals['correct']} / {totals['total']} |")
    lines.append("")
    lines.append("## Failure categories")
    lines.append("")
    if aggregate["failure_categories"]:
        lines.append("| Error code | Count | Example documents |")
        lines.append("| --- | ---: | --- |")
        for code, count in sorted(aggregate["failure_categories"].items(), key=lambda kv: -kv[1]):
            examples = ", ".join(aggregate["failure_examples"].get(code, []))
            lines.append(f"| {code} | {count} | {examples} |")
    else:
        lines.append("None.")
    lines.append("")
    lines.append("## Versions observed")
    for key, values in aggregate["versions_seen"].items():
        lines.append(f"- {key}: {values}")
    (run_dir / "aggregate-report.md").write_text("\n".join(lines) + "\n")


# --- orchestration --------------------------------------------------------------------

async def async_main(args: argparse.Namespace) -> None:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_name = f"run-{timestamp}-{args.mode}"
    run_dir = RESULTS_ROOT / run_name
    documents_dir = run_dir / "documents"
    logs_dir = run_dir / "logs"
    for path in (run_dir, documents_dir, logs_dir):
        path.mkdir(parents=True, exist_ok=True)

    check_postgres_healthy()
    check_alembic_head()
    check_no_stray_processes(allow_existing=args.allow_existing_processes)
    preflight_tests = () if args.skip_preflight_tests else tuple(args.preflight_tests or DEFAULT_PREFLIGHT_TESTS)
    run_preflight_tests(preflight_tests)

    from maximor.catalog.loader import load_catalog
    from maximor.db.models.statuses import CatalogStatus

    catalog_result = await load_catalog(
        catalog_path=CATALOG_PATH, organization_slug=f"eval-{run_name}",
        organization_name=f"Submission Evaluation {run_name}",
        catalog_version="eval-v1", catalog_status=CatalogStatus.ACTIVE,
    )
    organization_id = catalog_result["organization_id"]
    print(f"[setup] organization {organization_id}, {catalog_result['sku_count']} SKUs loaded")

    records = load_manifest()
    if args.mode == "pilot":
        selected = select_pilot_pdfs(records)
    else:
        selected = records
    records_by_form = {r.form_id: r for r in selected}

    manifest_out = {
        "run_name": run_name, "mode": args.mode, "workers": args.workers,
        "organization_id": organization_id, "started_at": datetime.now(UTC).isoformat(),
        "documents": [{"form_id": r.form_id, "slug": r.slug, "contract_item_count": r.contract_item_count} for r in selected],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest_out, indent=2, sort_keys=True))

    processes: list[ManagedProcess] = []
    try:
        processes.append(start_api(logs_dir))
        wait_for_api_health()
        processes.extend(start_workers(args.workers, logs_dir))

        uploaded = await upload_documents(organization_id, selected, manifest_out, run_dir / "manifest.json")

        await monitor_until_terminal(uploaded, run_dir, args.max_wait_minutes)
    finally:
        print("[shutdown] stopping only the processes this run started")
        shutdown_processes(processes)

    print("[reload] reloading every completed stage for each document")
    for form_id, info in uploaded.items():
        await reload_document(
            uuid.UUID(organization_id), uuid.UUID(info["document_id"]), form_id,
            documents_dir / info["slug"],
        )

    print("[evaluate] reading ground truth for offline evaluation")
    aggregate = run_offline_evaluation(records_by_form, documents_dir)
    write_aggregate_report(aggregate, run_dir)

    print(f"[done] results written to {run_dir}")
    print(f"[done] status counts: {aggregate['status_counts']}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("pilot", "full"), required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--max-wait-minutes", type=float, default=DEFAULT_MAX_WAIT_MINUTES)
    parser.add_argument("--skip-preflight-tests", action="store_true")
    parser.add_argument("--allow-existing-processes", action="store_true", help="Don't abort if a uvicorn/worker process matching this app is already running (e.g. an unrelated dev server on a different port).")
    parser.add_argument("--preflight-tests", nargs="*", default=None, help="Override the default focused pytest targets.")
    parser.add_argument("--i-understand-the-cost-risk", action="store_true", help=f"Required to pass --workers above {MAX_WORKERS_WITHOUT_OVERRIDE}.")
    args = parser.parse_args(argv)
    if args.workers > MAX_WORKERS_WITHOUT_OVERRIDE and not args.i_understand_the_cost_risk:
        parser.error(
            f"--workers {args.workers} exceeds the safety default of {MAX_WORKERS_WITHOUT_OVERRIDE} "
            "concurrent paid Claude sessions. Re-run with --i-understand-the-cost-risk to override "
            "only after a pilot/full run has already validated reliability at a lower concurrency."
        )
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # `local_storage_root` (and other config defaults) are relative paths
    # resolved against the process's own cwd. Pin it to REPO_ROOT so this
    # process resolves storage identically to the API/worker subprocesses it
    # launches (which are explicitly given cwd=REPO_ROOT below) regardless of
    # where the user invoked this script from.
    os.chdir(REPO_ROOT)
    asyncio.run(async_main(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
