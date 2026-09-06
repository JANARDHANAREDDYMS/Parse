"""Export one accepted analysis artifact for local inspection.

The command receives persisted organization/run identifiers, reloads the canonical
result through the persistence service, and writes deterministic JSON diagnostics.
It never opens the source PDF, invokes Claude, or mutates database state.
"""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from maximor.config import get_database_settings
from maximor.db.models import DocumentAnalysisRun
from maximor.db.session import get_session_factory
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.storage import LocalObjectStorage


def _parser() -> argparse.ArgumentParser:
    """Build the explicit, identifier-only export command arguments."""

    parser = argparse.ArgumentParser(description="Export one persisted document-analysis result.")
    parser.add_argument("--organization-id", required=True, type=UUID)
    parser.add_argument("--analysis-run-id", required=True, type=UUID)
    parser.add_argument("--output-dir", type=Path, default=Path("src/tests/test_results/document_analysis/of-0001"))
    parser.add_argument("--attempt-number", type=int, default=9)
    return parser


async def export_result(
    organization_id: UUID,
    analysis_run_id: UUID,
    output_dir: Path,
    attempt_number: int,
) -> tuple[Path, Path]:
    """Reload one completed run and write its result and safe structural summary."""

    settings = get_database_settings()
    sessions = get_session_factory()
    async with sessions() as session:
        run = await session.scalar(
            select(DocumentAnalysisRun).where(
                DocumentAnalysisRun.id == analysis_run_id,
                DocumentAnalysisRun.organization_id == organization_id,
            )
        )
        if run is None or run.status != "completed":
            raise RuntimeError("the requested analysis run is not completed")
        result = await DocumentAnalysisPersistenceService(
            sessions, LocalObjectStorage(settings.local_storage_root)
        ).load_completed_result(
            organization_id=organization_id,
            analysis_run_id=analysis_run_id,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / f"attempt-{attempt_number}-result.json"
    summary_path = output_dir / f"attempt-{attempt_number}-summary.json"
    result_json = json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    summary = {
        "analysis_run_id": str(analysis_run_id),
        "attempt_number": attempt_number,
        "validation_status": "validated",
        "collection_counts": {
            name: len(getattr(result, name))
            for name in (
                "contract_structure", "pricing_sections", "global_terms",
                "product_candidates", "commercial_statuses", "evidence_references",
            )
        },
    }
    summary_json = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    result_path.write_text(result_json, encoding="utf-8")
    summary_path.write_text(summary_json, encoding="utf-8")
    return result_path, summary_path


def main() -> None:
    """Run the explicit persisted-result export without PDF or Claude access."""

    args = _parser().parse_args()
    result_path, summary_path = asyncio.run(
        export_result(args.organization_id, args.analysis_run_id, args.output_dir, args.attempt_number)
    )
    print(json.dumps({"result": str(result_path), "summary": str(summary_path)}, sort_keys=True))


if __name__ == "__main__":
    main()
