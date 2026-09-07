"""Run one document through a fresh organization and capture every stage's output for a demo.

Creates a brand-new organization + catalog, uploads one PDF through the real
HTTP API (an API + worker pool must already be running -- this does not start
its own), waits for the pipeline to reach a terminal state, then writes both
the raw persisted JSON and a human-readable Markdown report for every stage
(preprocessing, document analysis, SKU mapping, term applicability,
normalization) under tests/test_results/end_to_end/<form>/<timestamp>/,
matching this repo's existing end-to-end capture convention.

Usage (from repo root, with an API + worker already running):
    PYTHONPATH=src src/.venv/bin/python src/scripts/capture_pipeline_demo.py \\
        --pdf data/synthetic_order_form_dataset_50/pdfs/of-0030.pdf \\
        --api-base http://127.0.0.1:8002
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG_PATH = REPO_ROOT / "data" / "synthetic_order_form_dataset_50" / "sku_catalog.json"
POLL_INTERVAL_SECONDS = 5
DEFAULT_MAX_WAIT_MINUTES = 20
TERMINAL_JOB_STATUSES = {"completed", "failed"}
NORMALIZATION_TERMINAL_STATUSES = {"completed", "review_required", "failed_validation", "failed"}


def _money(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, dict):
        return f"{value.get('amount')} {value.get('currency_code')}"
    return str(value)


async def upload_document(api_base: str, organization_id: str, pdf_path: Path) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as client:
        with pdf_path.open("rb") as fh:
            response = await client.post(
                f"{api_base}/v1/organizations/{organization_id}/documents",
                files={"file": (pdf_path.name, fh, "application/pdf")},
            )
        response.raise_for_status()
        return response.json()


async def wait_for_terminal(api_base: str, organization_id: str, document_id: str, max_wait_minutes: float) -> None:
    import time
    deadline = time.monotonic() + max_wait_minutes * 60
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            response = await client.get(f"{api_base}/v1/organizations/{organization_id}/documents/{document_id}/pipeline")
            response.raise_for_status()
            pipeline = response.json()
            preprocessing_status = (pipeline.get("preprocessing") or {}).get("status")
            analysis_status = (pipeline.get("document_analysis") or {}).get("status")
            term_status = (pipeline.get("term_applicability") or {}).get("status")
            norm_status = (pipeline.get("normalization") or {}).get("status")
            print(f"[monitor] preprocessing={preprocessing_status} analysis={analysis_status} term_applicability={term_status} normalization={norm_status}")
            if norm_status in NORMALIZATION_TERMINAL_STATUSES:
                print(f"[monitor] terminal: normalization status = {norm_status}")
                return
            if preprocessing_status == "failed" or analysis_status == "failed":
                print(f"[monitor] terminal: an earlier stage failed (preprocessing={preprocessing_status}, analysis={analysis_status}) -- chain stops here")
                return
            if time.monotonic() >= deadline:
                print(f"[monitor] max wait of {max_wait_minutes} minutes reached -- stopping, capturing whatever is available")
                return
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def write_json(path: Path, payload) -> None:
    write(path, json.dumps(payload, indent=2, sort_keys=True, default=str))


async def capture(organization_id: uuid.UUID, document_id: uuid.UUID, out_dir: Path) -> dict:
    from maximor.config import get_database_settings
    from maximor.db.models import DocumentAnalysisRun, TermApplicabilityRun, SkuMappingRun, ProcessingJob
    from maximor.db.session import get_session_factory
    from maximor.storage import LocalObjectStorage
    from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
    from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
    from maximor.sku_mapping.persistence import SkuMappingPersistenceService
    from maximor.normalization.persistence import NormalizationPersistenceService
    from maximor.preprocessing.persistence import PreprocessingResultRepository
    from sqlalchemy import select

    settings = get_database_settings()
    sessions = get_session_factory()
    storage = LocalObjectStorage(settings.local_storage_root)
    analysis_service = DocumentAnalysisPersistenceService(sessions, storage)
    term_service = TermApplicabilityPersistenceService(sessions, storage)
    sku_service = SkuMappingPersistenceService(sessions, storage)
    normalization_service = NormalizationPersistenceService(sessions, storage)
    prep_repo = PreprocessingResultRepository(sessions, storage)

    summary = {"organization_id": str(organization_id), "document_id": str(document_id), "captured_at": datetime.now(UTC).isoformat()}

    async with sessions() as session:
        jobs = (await session.scalars(select(ProcessingJob).where(ProcessingJob.document_id == document_id).order_by(ProcessingJob.created_at))).all()
    summary["jobs"] = [
        {"job_type": j.job_type, "status": j.status, "attempt_number": j.attempt_number, "error_code": j.error_code, "error_message": j.error_message}
        for j in jobs
    ]
    write_json(out_dir / "00-pipeline-summary.json", summary)
    pipeline_lines = ["# Pipeline summary", "", f"- organization_id: `{organization_id}`", f"- document_id: `{document_id}`", f"- captured_at: {summary['captured_at']}", "", "| Stage | Status | Attempt | Error |", "| --- | --- | ---: | --- |"]
    for j in summary["jobs"]:
        pipeline_lines.append(f"| {j['job_type']} | {j['status']} | {j['attempt_number']} | {j['error_code'] or '—'} |")
    write(out_dir / "00-pipeline-summary.md", "\n".join(pipeline_lines) + "\n")

    async with sessions() as session:
        arun = await session.scalar(select(DocumentAnalysisRun).where(DocumentAnalysisRun.organization_id == organization_id, DocumentAnalysisRun.document_id == document_id).order_by(DocumentAnalysisRun.attempt_number.desc()))

    if arun is not None:
        prep = await prep_repo.load_preprocessed_document(organization_id=organization_id, run_id=arun.preprocessing_run_id)
        write_json(out_dir / "01-preprocessing-summary.json", {"pages": len(prep.pages), "processor_version": prep.processor_version})
        lines = ["# Preprocessing", "", f"- pages: {len(prep.pages)}", f"- processor_version: {prep.processor_version}", "", "## Native text per page", ""]
        for page in prep.pages:
            lines.append(f"### Page {page.page_number}\n")
            lines.append("```")
            lines.append(page.native_text.plain_text)
            lines.append("```\n")
        write(out_dir / "01-preprocessing-report.md", "\n".join(lines))

    if arun is not None and arun.status == "completed":
        analysis = await analysis_service.load_completed_result(organization_id=organization_id, analysis_run_id=arun.id)
        write_json(out_dir / "02-document-analysis-result.json", json.loads(analysis.model_dump_json()))
        lines = ["# Document analysis", "", f"- schema_version: {analysis.schema_version}", f"- agent_version: {analysis.agent_version}", "", "## Product candidates", "", "| candidate_id | raw_name | raw_attributes |", "| --- | --- | --- |"]
        for c in analysis.product_candidates:
            lines.append(f"| {c.candidate_id} | {c.raw_name} | {c.raw_attributes or {}} |")
        lines += ["", "## Commercial status", "", "| candidate_id | status |", "| --- | --- |"]
        for s in analysis.commercial_statuses:
            lines.append(f"| {s.candidate_id} | {s.status} |")
        lines += ["", "## Global terms", "", "| term_id | raw_name | raw_value | scope | applies_to |", "| --- | --- | --- | --- | --- |"]
        for t in analysis.global_terms:
            lines.append(f"| {t.term_id} | {t.raw_name} | {t.raw_value} | {t.applicability_scope} | {', '.join(t.applies_to_candidate_ids) or '—'} |")
        write(out_dir / "02-document-analysis-report.md", "\n".join(lines) + "\n")
    elif arun is not None:
        write_json(out_dir / "02-document-analysis-result.json", {"status": arun.status, "error_code": arun.error_code, "error_message": arun.error_message})
        write(out_dir / "02-document-analysis-report.md", f"# Document analysis\n\nStatus: **{arun.status}**\nError: `{arun.error_code}` -- {arun.error_message}\n")

    async with sessions() as session:
        sku_runs = (await session.scalars(select(SkuMappingRun).where(SkuMappingRun.organization_id == organization_id, SkuMappingRun.document_id == document_id))).all()
    sku_dump, sku_lines = [], ["# SKU mapping", "", "| candidate | outcome | sku_code | sku_name |", "| --- | --- | --- | --- |"]
    for run in sku_runs:
        if run.status == "completed":
            artifact = await sku_service.load_completed_result(organization_id=organization_id, run_id=run.id)
            sku_dump.append(json.loads(artifact.model_dump_json()))
            sku_lines.append(f"| {artifact.task.candidate_id} | {artifact.decision.outcome} | {artifact.decision.sku_code or '—'} | {artifact.decision.sku_name or '—'} |")
        else:
            sku_lines.append(f"| (run {run.id}) | {run.status} | — | error: {run.error_code} |")
    write_json(out_dir / "03-sku-mapping-results.json", sku_dump)
    write(out_dir / "03-sku-mapping-report.md", "\n".join(sku_lines) + "\n")

    async with sessions() as session:
        trun = (await session.scalars(select(TermApplicabilityRun).where(TermApplicabilityRun.organization_id == organization_id, TermApplicabilityRun.document_id == document_id).order_by(TermApplicabilityRun.attempt_number.desc()))).first()
    if trun is not None and trun.status == "completed":
        combined = await term_service.load_completed_result(organization_id=organization_id, run_id=trun.id)
        applicability = combined.applicability
        write_json(out_dir / "04-term-applicability-result.json", json.loads(combined.model_dump_json()))
        lines = ["# Term applicability", "", "## Triage", "", "| term_id | disposition |", "| --- | --- |"]
        for d in combined.triage.decisions:
            lines.append(f"| {d.term_id} | {d.disposition} |")
        lines += ["", "## Applicability decisions", "", "| term_id | disposition | scope | candidates |", "| --- | --- | --- | --- |"]
        for d in applicability.decisions:
            lines.append(f"| {d.term_id} | {d.disposition} | {d.applicability_scope} | {', '.join(d.applies_to_candidate_ids) or '—'} |")
        lines += ["", "## Extracted commercial facts", "", "| candidate_id | field | raw_value |", "| --- | --- | --- |"]
        for bundle in applicability.candidate_commercial_facts:
            for fact in bundle.facts:
                lines.append(f"| {bundle.candidate_id} | {fact.field} | {fact.raw_value} |")
        write(out_dir / "04-term-applicability-report.md", "\n".join(lines) + "\n")
    elif trun is not None:
        write_json(out_dir / "04-term-applicability-result.json", {"status": trun.status, "error_code": trun.error_code})
        write(out_dir / "04-term-applicability-report.md", f"# Term applicability\n\nStatus: **{trun.status}**\nError: `{trun.error_code}` -- {trun.error_message}\n")

    norm_run = await normalization_service.latest_run(organization_id=organization_id, document_id=document_id)
    if norm_run is not None and norm_run.status in NORMALIZATION_TERMINAL_STATUSES and norm_run.status != "failed":
        result = await normalization_service.load_completed_result(organization_id=organization_id, run_id=norm_run.id)
        write_json(out_dir / "05-normalization-result.json", json.loads(result.model_dump_json()))
        lines = ["# Normalization", "", f"- status: {result.status.value}", "", "## Line items", ""]
        for item in result.extraction.line_items:
            lines.append(f"### {item.sku_code or '(unmapped)'} — {item.sku_name or ''}")
            lines.append("")
            lines.append(f"- quantity: {item.quantity}")
            lines.append(f"- currency: {item.currency}")
            lines.append(f"- unit_price: {_money(json.loads(item.model_dump_json()).get('unit_price'))}")
            lines.append(f"- total_listed_value: {_money(json.loads(item.model_dump_json()).get('total_listed_value'))}")
            lines.append(f"- service_start_date: {item.service_start_date}")
            lines.append(f"- service_end_date: {item.service_end_date}")
            lines.append(f"- invoicing_schedule_type: {item.invoicing_schedule_type}")
            lines.append(f"- invoicing_frequency: {item.invoicing_frequency}")
            lines.append(f"- payment_terms: {item.payment_terms}")
            lines.append(f"- special_notes: {item.special_notes}")
            lines.append("")
        lines += ["## Finalization issues", "", "| code | classification | hard | candidate | field |", "| --- | --- | --- | --- | --- |"]
        for issue in result.finalization_issues:
            lines.append(f"| {issue.code} | {issue.classification} | {issue.hard} | {issue.candidate_id or '—'} | {issue.field_name or '—'} |")
        write(out_dir / "05-normalization-report.md", "\n".join(lines) + "\n")
    elif norm_run is not None:
        write_json(out_dir / "05-normalization-result.json", {"status": norm_run.status, "error_code": norm_run.error_code, "error_message": norm_run.error_message})
        write(out_dir / "05-normalization-report.md", f"# Normalization\n\nStatus: **{norm_run.status}**\nError: `{norm_run.error_code}` -- {norm_run.error_message}\n")
    else:
        write(out_dir / "05-normalization-report.md", "# Normalization\n\nNo normalization run exists for this document (an earlier stage likely never completed).\n")

    return summary


async def async_main(args: argparse.Namespace) -> None:
    from maximor.catalog.loader import load_catalog
    from maximor.db.models.statuses import CatalogStatus

    pdf_path = Path(args.pdf)
    if not pdf_path.is_absolute():
        pdf_path = REPO_ROOT / pdf_path
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    org_slug = f"demo-capture-{pdf_path.stem}-{timestamp}"
    catalog_result = await load_catalog(
        catalog_path=Path(args.catalog), organization_slug=org_slug,
        organization_name=f"Demo Capture {pdf_path.stem} {timestamp}",
        catalog_version="demo-v1", catalog_status=CatalogStatus.ACTIVE,
    )
    organization_id = catalog_result["organization_id"]
    print(f"[setup] new organization {organization_id} ({org_slug}), {catalog_result['sku_count']} SKUs loaded")

    uploaded = await upload_document(args.api_base, organization_id, pdf_path)
    document_id = uploaded["document_id"]
    print(f"[upload] {pdf_path.name} -> document {document_id}")

    await wait_for_terminal(args.api_base, organization_id, document_id, args.max_wait_minutes)

    out_dir = REPO_ROOT / "src" / "tests" / "test_results" / "end_to_end" / pdf_path.stem.upper() / timestamp
    summary = await capture(uuid.UUID(organization_id), uuid.UUID(document_id), out_dir)
    write_json(out_dir / "run-info.json", {"organization_id": organization_id, "document_id": document_id, "pdf": str(pdf_path), "api_base": args.api_base})
    print(f"[done] captured to {out_dir}")
    print(f"[done] jobs: {summary['jobs']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG_PATH))
    parser.add_argument("--max-wait-minutes", type=float, default=DEFAULT_MAX_WAIT_MINUTES)
    return parser.parse_args()


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(async_main(parse_args())))
