import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maximor.db.models import ProcessingJob
from maximor.jobs.repository import ClaimedJob, JobRepository
from maximor.jobs.types import JobType
from maximor.storage import ObjectStorage


class OrganizationNotFoundError(Exception):
    pass


class DuplicateDocumentError(Exception):
    pass


@dataclass(frozen=True)
class CreatedDocumentJob:
    organization_id: uuid.UUID
    document_id: uuid.UUID
    job_id: uuid.UUID
    document_status: str
    job_status: str


async def create_document_job(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    storage: ObjectStorage,
    organization_id: uuid.UUID,
    original_filename: str,
    media_type: str,
    chunks,
    maximum_bytes: int,
    job_type: JobType,
) -> CreatedDocumentJob:
    async with session_factory() as session:
        if not await JobRepository(session).organization_exists(organization_id):
            raise OrganizationNotFoundError

    document_id = uuid.uuid4()
    job_id = uuid.uuid4()
    storage_key = f"organizations/{organization_id}/documents/{document_id}.pdf"
    metadata = await storage.put(storage_key, chunks, maximum_bytes=maximum_bytes)
    try:
        async with session_factory() as session:
            async with session.begin():
                document, job = JobRepository(session).add_document_and_job(
                    organization_id=organization_id,
                    document_id=document_id,
                    job_id=job_id,
                    original_filename=original_filename,
                    storage_key=storage_key,
                    sha256_checksum=metadata.sha256_checksum,
                    media_type=media_type,
                    file_size=metadata.size,
                    job_type=job_type,
                )
                await session.flush()
                result = CreatedDocumentJob(
                    organization_id, document.id, job.id, document.status, job.status
                )
    except IntegrityError as exc:
        await storage.delete(storage_key)
        if "uq_documents_organization_checksum" in str(exc.orig):
            raise DuplicateDocumentError from exc
        raise
    except BaseException:
        await storage.delete(storage_key)
        raise
    return result


async def get_tenant_job(
    session_factory: async_sessionmaker[AsyncSession],
    organization_id: uuid.UUID,
    job_id: uuid.UUID,
) -> ProcessingJob | None:
    async with session_factory() as session:
        return await JobRepository(session).get_job(organization_id, job_id)


async def schedule_document_analysis_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    preprocessing_run_id: uuid.UUID,
    retry_terminal: bool = False,
) -> ProcessingJob:
    """Create or return the one analysis job bound to one completed preprocessing run."""
    async with session_factory() as session:
        try:
            async with session.begin():
                existing = await session.scalar(
                    select(ProcessingJob).where(
                        ProcessingJob.preprocessing_run_id == preprocessing_run_id,
                        ProcessingJob.job_type == JobType.DOCUMENT_ANALYSIS.value,
                    )
                )
                if existing is not None and (not retry_terminal or existing.status in {"queued", "running"}):
                    return existing
                attempts = list((await session.scalars(select(ProcessingJob.attempt_number).where(
                    ProcessingJob.preprocessing_run_id == preprocessing_run_id,
                    ProcessingJob.job_type == JobType.DOCUMENT_ANALYSIS.value,
                ))).all())
                job = ProcessingJob(
                    id=uuid.uuid4(), organization_id=organization_id,
                    document_id=document_id, preprocessing_run_id=preprocessing_run_id,
                    job_type=JobType.DOCUMENT_ANALYSIS.value, status="queued",
                    attempt_number=(max(attempts) + 1) if attempts else 1,
                )
                session.add(job)
                await session.flush()
                return job
        except IntegrityError:
            async with session.begin():
                existing = await session.scalar(
                    select(ProcessingJob).where(
                        ProcessingJob.preprocessing_run_id == preprocessing_run_id,
                        ProcessingJob.job_type == JobType.DOCUMENT_ANALYSIS.value,
                    )
                )
                if existing is not None:
                    return existing
            raise


async def claim_next_job(
    session_factory: async_sessionmaker[AsyncSession],
    eligible_job_types: tuple[JobType, ...],
) -> ClaimedJob | None:
    """Claim and commit one job whose type has a registered worker handler."""

    async with session_factory() as session:
        async with session.begin():
            return await JobRepository(session).claim_next(
                [job_type.value for job_type in eligible_job_types]
            )


async def mark_job_completed(
    session_factory: async_sessionmaker[AsyncSession], claimed: ClaimedJob
) -> None:
    """Persist the runner-owned successful terminal status for one claimed job."""

    async with session_factory() as session:
        async with session.begin():
            await JobRepository(session).mark_completed(claimed)


async def mark_job_failed(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedJob,
    *,
    error_code: str,
    error_message: str,
) -> None:
    """Persist the runner-owned failed terminal status with safe error details."""

    async with session_factory() as session:
        async with session.begin():
            await JobRepository(session).mark_failed(
                claimed, error_code=error_code, error_message=error_message
            )
