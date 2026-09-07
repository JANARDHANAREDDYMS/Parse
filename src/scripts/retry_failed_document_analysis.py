"""Re-queue `document_analysis` for documents whose latest attempt failed.

Reuses the existing document_id and preprocessing_run_id -- preprocessing is
not redone and no new document is uploaded. A worker process must be running
(with whatever DOCUMENT_ANALYSIS_TIMEOUT_SECONDS it was started with) to pick
up the newly queued jobs.

Usage (from repo root):
    PYTHONPATH=src src/.venv/bin/python src/scripts/retry_failed_document_analysis.py \\
        --organization-id <uuid>

    # Only specific documents:
    PYTHONPATH=src src/.venv/bin/python src/scripts/retry_failed_document_analysis.py \\
        --organization-id <uuid> --document-id <uuid> --document-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select

from maximor.db.models import DocumentAnalysisRun
from maximor.db.session import get_session_factory
from maximor.jobs.service import schedule_document_analysis_job


async def main(organization_id: uuid.UUID, document_ids: list[uuid.UUID] | None) -> None:
    sessions = get_session_factory()
    async with sessions() as session:
        query = select(DocumentAnalysisRun).where(
            DocumentAnalysisRun.organization_id == organization_id,
            DocumentAnalysisRun.status == "failed",
        )
        if document_ids:
            query = query.where(DocumentAnalysisRun.document_id.in_(document_ids))
        failed_runs = (await session.scalars(query)).all()

    if not failed_runs:
        print("No failed document_analysis runs found for this organization (and document filter, if given).")
        return

    for run in failed_runs:
        job = await schedule_document_analysis_job(
            sessions, organization_id=organization_id, document_id=run.document_id,
            preprocessing_run_id=run.preprocessing_run_id, retry_terminal=True,
        )
        print(f"document_id={run.document_id} previous_error={run.error_code} -> queued job_id={job.id} attempt={job.attempt_number}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--organization-id", required=True, type=uuid.UUID)
    parser.add_argument("--document-id", action="append", type=uuid.UUID, dest="document_ids", help="Repeatable. Omit to retry every failed document_analysis run in the organization.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.organization_id, args.document_ids))
