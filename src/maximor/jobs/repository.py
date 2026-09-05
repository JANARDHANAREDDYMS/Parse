import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from maximor.db.models import Document, Organization, ProcessingJob
from maximor.db.models.statuses import DocumentStatus, ProcessingJobStatus
from maximor.jobs.types import JobContext, JobType


@dataclass(frozen=True)
class ClaimedJob:
    job_id: uuid.UUID
    organization_id: uuid.UUID
    document_id: uuid.UUID
    job_type: str
    attempt_number: int

    def to_context(self) -> JobContext:
        """Convert the committed database claim into an immutable handler context."""

        return JobContext(
            organization_id=self.organization_id,
            document_id=self.document_id,
            processing_job_id=self.job_id,
            job_type=self.job_type,
            attempt_number=self.attempt_number,
        )


class JobRepository:
    """Perform tenant-scoped processing-job persistence through one async session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def organization_exists(self, organization_id: uuid.UUID) -> bool:
        return (
            await self._session.scalar(
                select(Organization.id).where(Organization.id == organization_id)
            )
        ) is not None

    def add_document_and_job(
        self,
        *,
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
        job_id: uuid.UUID,
        original_filename: str,
        storage_key: str,
        sha256_checksum: str,
        media_type: str,
        file_size: int,
        job_type: JobType,
    ) -> tuple[Document, ProcessingJob]:
        document = Document(
            id=document_id,
            organization_id=organization_id,
            original_filename=original_filename,
            storage_key=storage_key,
            sha256_checksum=sha256_checksum,
            media_type=media_type,
            file_size=file_size,
            status=DocumentStatus.UPLOADED.value,
        )
        job = ProcessingJob(
            id=job_id,
            organization_id=organization_id,
            document_id=document_id,
            job_type=job_type.value,
            status=ProcessingJobStatus.QUEUED.value,
            attempt_number=1,
        )
        self._session.add_all([document, job])
        return document, job

    async def get_job(
        self, organization_id: uuid.UUID, job_id: uuid.UUID
    ) -> ProcessingJob | None:
        return await self._session.scalar(
            select(ProcessingJob).where(
                ProcessingJob.organization_id == organization_id,
                ProcessingJob.id == job_id,
            )
        )

    async def claim_next(
        self, eligible_job_types: Sequence[str]
    ) -> ClaimedJob | None:
        """Lock and transition the oldest eligible queued job to running."""

        if not eligible_job_types:
            return None
        job = await self._session.scalar(
            select(ProcessingJob)
            .where(
                ProcessingJob.job_type.in_(eligible_job_types),
                ProcessingJob.status == ProcessingJobStatus.QUEUED.value,
            )
            .order_by(ProcessingJob.created_at, ProcessingJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        job.status = ProcessingJobStatus.RUNNING.value
        job.started_at = datetime.now(UTC)
        job.completed_at = None
        job.error_code = None
        job.error_message = None
        await self._session.flush()
        return ClaimedJob(
            job.id,
            job.organization_id,
            job.document_id,
            job.job_type,
            job.attempt_number,
        )

    async def get_document_for_job(self, claimed: ClaimedJob) -> Document | None:
        return await self._session.scalar(
            select(Document).where(
                Document.organization_id == claimed.organization_id,
                Document.id == claimed.document_id,
            )
        )

    async def mark_completed(self, claimed: ClaimedJob) -> None:
        job = await self.get_job(claimed.organization_id, claimed.job_id)
        if job is None:
            return
        job.status = ProcessingJobStatus.COMPLETED.value
        job.completed_at = datetime.now(UTC)
        job.error_code = None
        job.error_message = None

    async def mark_failed(
        self, claimed: ClaimedJob, *, error_code: str, error_message: str
    ) -> None:
        job = await self.get_job(claimed.organization_id, claimed.job_id)
        if job is None:
            return
        job.status = ProcessingJobStatus.FAILED.value
        job.completed_at = datetime.now(UTC)
        job.error_code = error_code[:100]
        job.error_message = error_message
