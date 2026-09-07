"""Safely load one tenant-scoped, completed document-analysis result for term applicability.

This never reopens the PDF, never accesses raw preprocessing artifacts, and
exposes no SQL or session to callers. It reuses
`DocumentAnalysisPersistenceService.load_completed_result` — which already
checksum-verifies and Pydantic-revalidates the canonical artifact — for the
actual trusted content, and adds only the explicit identity confirmation
this agent needs before building a task from it.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maximor.db.models import DocumentAnalysisRun
from maximor.document_analysis.errors import DocumentAnalysisError
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.term_applicability.contracts import TermApplicabilityTask, build_term_applicability_task
from maximor.term_applicability.errors import (
    TermApplicabilityAnalysisRunNotCompletedError,
    TermApplicabilityAnalysisRunNotFoundError,
    TermApplicabilitySourceMismatchError,
)


class TermApplicabilityRepository:
    """Load one trusted `TermApplicabilityTask` for one completed analysis run."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        analysis_results: DocumentAnalysisPersistenceService,
    ) -> None:
        """Receive the application session factory and the existing analysis persistence service."""

        self._sessions = sessions
        self._analysis_results = analysis_results

    async def load_task(
        self,
        *,
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
        preprocessing_run_id: uuid.UUID,
        analysis_run_id: uuid.UUID,
        schema_version: str,
    ) -> TermApplicabilityTask:
        """Confirm tenant/run identity, load the canonical result, and build one task.

        Confirms, in order: the analysis run exists for this organization and
        document; it has reached `completed` status; and the canonical
        result's own `document_id`/`preprocessing_run_id` (already
        checksum-verified by `load_completed_result`) agree with what was
        requested. Only then is a `TermApplicabilityTask` built from it.
        """

        async with self._sessions() as session:
            run = await session.scalar(
                select(DocumentAnalysisRun).where(
                    DocumentAnalysisRun.id == analysis_run_id,
                    DocumentAnalysisRun.organization_id == organization_id,
                    DocumentAnalysisRun.document_id == document_id,
                )
            )
        if run is None:
            raise TermApplicabilityAnalysisRunNotFoundError
        if run.status != "completed":
            raise TermApplicabilityAnalysisRunNotCompletedError

        try:
            result = await self._analysis_results.load_completed_result(
                organization_id=organization_id, analysis_run_id=analysis_run_id,
            )
        except DocumentAnalysisError:
            raise TermApplicabilityAnalysisRunNotFoundError from None

        if result.document_id != document_id or result.preprocessing_run_id != preprocessing_run_id:
            raise TermApplicabilitySourceMismatchError

        return build_term_applicability_task(
            result=result, analysis_run_id=analysis_run_id, schema_version=schema_version,
        )
