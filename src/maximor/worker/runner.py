"""Poll PostgreSQL, dispatch registered jobs, and own every final status transition."""

import asyncio
import signal

import structlog
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maximor.config import DatabaseSettings
from maximor.db.models import ProcessingJob
from maximor.jobs.dispatcher import JobDispatcher
from maximor.jobs.errors import InvalidJobContextError, JobExecutionError
from maximor.jobs.repository import ClaimedJob
from maximor.jobs.service import claim_next_job, mark_job_completed, mark_job_failed, schedule_document_analysis_job
from maximor.jobs.types import JobType

logger = structlog.get_logger()


class WorkerRunner:
    """Claim registered jobs, invoke one handler, and persist their terminal status."""

    def __init__(
        self,
        *,
        settings: DatabaseSettings,
        session_factory: async_sessionmaker[AsyncSession],
        dispatcher: JobDispatcher,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._dispatcher = dispatcher

    async def process_one(self) -> bool:
        """Claim and process at most one job, returning whether a claim occurred."""

        claimed = await claim_next_job(
            self._session_factory, self._dispatcher.registered_job_types
        )
        if claimed is None:
            return False
        identifiers = self._safe_identifiers(claimed)
        logger.info("job_claimed", status="running", **identifiers)
        try:
            try:
                context = claimed.to_context()
            except ValueError:
                raise InvalidJobContextError from None
            await self._dispatcher.dispatch(context)
        except JobExecutionError as exc:
            await mark_job_failed(
                self._session_factory,
                claimed,
                error_code=exc.code,
                error_message=exc.safe_message,
            )
            logger.error(
                "job_failed",
                status="failed",
                error_code=exc.code,
                exception_class=exc.__class__.__name__,
                **identifiers,
            )
            if self._is_retryable_timeout(claimed, exc.code):
                await self._retry_document_analysis(claimed)
        except Exception as exc:
            await mark_job_failed(
                self._session_factory,
                claimed,
                error_code="worker_operation_failed",
                error_message="The worker operation failed.",
            )
            logger.error(
                "job_failed",
                status="failed",
                error_code="worker_operation_failed",
                exception_class=exc.__class__.__name__,
                **identifiers,
            )
        else:
            await mark_job_completed(self._session_factory, claimed)
            logger.info("job_completed", status="completed", **identifiers)
        return True

    async def run(self, *, once: bool = False) -> int:
        """Poll without busy waiting, or exit after at most one claim in once mode."""

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signal_name, stop_event.set)
            except NotImplementedError:
                pass

        logger.info(
            "worker_started", worker_identity=self._settings.worker_identity, once=once
        )
        while not stop_event.is_set():
            try:
                processed = await self.process_one()
            except SQLAlchemyError as exc:
                logger.error(
                    "job_claim_failed",
                    worker_identity=self._settings.worker_identity,
                    exception_class=exc.__class__.__name__,
                )
                if once:
                    return 1
                processed = False
            if once:
                if not processed:
                    logger.info(
                        "worker_no_job",
                        worker_identity=self._settings.worker_identity,
                    )
                return 0
            if not processed:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self._settings.worker_poll_interval_seconds,
                    )
                except TimeoutError:
                    continue

        logger.info(
            "worker_stopped", worker_identity=self._settings.worker_identity
        )
        return 0

    def _is_retryable_timeout(self, claimed: ClaimedJob, error_code: str) -> bool:
        """Only a `document_analysis` timeout gets an automatic retry.

        A timeout is transient -- API latency under concurrent load, not a
        data or logic defect -- so one retry with the same input often
        succeeds. Every other error code (validation failures, malformed
        output, etc.) would just fail identically again, so retrying those
        would only waste money; they stay single-attempt.
        """
        return (
            claimed.job_type == JobType.DOCUMENT_ANALYSIS.value
            and error_code == "document_analysis_timeout"
            and claimed.attempt_number < self._settings.document_analysis_timeout_max_attempts
        )

    async def _retry_document_analysis(self, claimed: ClaimedJob) -> None:
        """Queue exactly one more `document_analysis` attempt after a timeout.

        Reuses the same `preprocessing_run_id` -- preprocessing already
        succeeded and is not redone, and no new document is uploaded.
        `schedule_document_analysis_job` is safe to call here because the
        just-failed job row already freed the one-in-flight-per-preprocessing-run
        slot that a `queued`/`running` attempt would otherwise hold.
        """
        async with self._session_factory() as session:
            job = await session.scalar(select(ProcessingJob).where(
                ProcessingJob.id == claimed.job_id, ProcessingJob.organization_id == claimed.organization_id,
            ))
        if job is None or job.preprocessing_run_id is None:
            return
        retry_job = await schedule_document_analysis_job(
            self._session_factory, organization_id=claimed.organization_id, document_id=claimed.document_id,
            preprocessing_run_id=job.preprocessing_run_id, retry_terminal=True,
        )
        logger.info(
            "document_analysis_timeout_retry_queued",
            organization_id=str(claimed.organization_id), document_id=str(claimed.document_id),
            retry_job_id=str(retry_job.id), retry_attempt_number=retry_job.attempt_number,
        )

    def _safe_identifiers(self, claimed: ClaimedJob) -> dict[str, str | int]:
        return {
            "worker_identity": self._settings.worker_identity,
            "organization_id": str(claimed.organization_id),
            "document_id": str(claimed.document_id),
            "job_id": str(claimed.job_id),
            "job_type": claimed.job_type,
            "attempt_number": claimed.attempt_number,
        }
