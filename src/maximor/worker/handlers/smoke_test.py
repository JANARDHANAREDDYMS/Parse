"""Verify one claimed document's stored size/checksum without inspecting PDF content."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maximor.jobs.errors import (
    ChecksumMismatchError,
    DocumentNotFoundError,
    SizeMismatchError,
    StoredObjectMissingError,
)
from maximor.jobs.repository import ClaimedJob, JobRepository
from maximor.jobs.types import JobContext
from maximor.storage import ObjectStorage, StorageError


class PipelineSmokeTestHandler:
    """Check tenant-scoped object integrity and leave all job status changes to runner."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage

    async def execute(self, context: JobContext) -> None:
        """Raise a typed failure unless the stored object's size and checksum match."""

        claimed = ClaimedJob(
            job_id=context.processing_job_id,
            organization_id=context.organization_id,
            document_id=context.document_id,
            job_type=context.job_type,
            attempt_number=context.attempt_number,
        )
        async with self._session_factory() as session:
            document = await JobRepository(session).get_document_for_job(claimed)
            if document is None:
                raise DocumentNotFoundError
            storage_key = document.storage_key
            expected_checksum = document.sha256_checksum
            expected_size = document.file_size

        try:
            actual = await self._storage.inspect(storage_key)
        except StorageError as exc:
            if exc.code == "stored_object_missing":
                raise StoredObjectMissingError from None
            raise

        if actual.sha256_checksum != expected_checksum:
            raise ChecksumMismatchError
        if expected_size is not None and actual.size != expected_size:
            raise SizeMismatchError
