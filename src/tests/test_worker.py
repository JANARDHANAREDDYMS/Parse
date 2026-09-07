"""Exercise worker routing and status ownership without inspecting PDF content."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from maximor.config import get_database_settings
from maximor.db.models import Document, DocumentProcessingRun, ProcessingJob
from maximor.db.session import get_session_factory
from maximor.jobs.dispatcher import JobDispatcher
from maximor.jobs.errors import JobExecutionError, UnsupportedJobTypeError
from maximor.jobs.repository import ClaimedJob
from maximor.jobs.service import claim_next_job, create_document_job, get_tenant_job, mark_job_failed
from maximor.jobs.types import JobContext, JobType
from maximor.storage import LocalObjectStorage
from maximor.worker.handlers import PipelineSmokeTestHandler
from maximor.worker.runner import WorkerRunner


async def byte_stream(value: bytes):
    """Yield one in-memory test chunk without reading repository dataset files."""
    yield value


async def create_job(storage, organization, pdf_bytes):
    """Create one real smoke job through the same application service as FastAPI."""
    return await create_document_job(
        session_factory=get_session_factory(), storage=storage,
        organization_id=organization, original_filename="order.pdf",
            media_type="application/pdf", chunks=byte_stream(pdf_bytes), maximum_bytes=1024,
            job_type=JobType.PIPELINE_SMOKE_TEST,
    )


def make_context(job_type: str = JobType.PIPELINE_SMOKE_TEST.value) -> JobContext:
    """Build an immutable dispatcher context with safe synthetic UUIDs."""
    return JobContext(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), job_type, 1)


class RecordingHandler:
    """Record test-only dispatch calls and optionally raise a typed failure."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.calls: list[JobContext] = []
        self.failure = failure

    async def execute(self, context: JobContext) -> None:
        """Record exactly one context and raise only when configured by a test."""
        self.calls.append(context)
        if self.failure is not None:
            raise self.failure


async def seed_document_analysis_job(organization_id: uuid.UUID, *, attempt_number: int = 1) -> ClaimedJob:
    """Create a document + completed preprocessing run + one `running` `document_analysis` job.

    Status is `running` (not `queued`) because these tests drive the retry
    logic directly via a hand-built `ClaimedJob` rather than through
    `claim_next_job` -- that claims the oldest queued job of a type across
    the *entire* shared database, which would risk stealing or colliding
    with a real, separately-running pipeline's own document_analysis jobs.
    Returns a `ClaimedJob` ready to pass straight to the runner/service calls
    these tests exercise.
    """
    document_id, preprocessing_job_id, preprocessing_run_id, analysis_job_id = (uuid.uuid4() for _ in range(4))
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        async with session.begin():
            session.add(Document(id=document_id, organization_id=organization_id, original_filename="order.pdf", storage_key=f"generated/{document_id}.pdf", sha256_checksum="a" * 64, media_type="application/pdf", status="completed"))
            session.add(ProcessingJob(id=preprocessing_job_id, organization_id=organization_id, document_id=document_id, job_type="document_preprocessing", status="completed", attempt_number=1))
            await session.flush()
            session.add(DocumentProcessingRun(id=preprocessing_run_id, organization_id=organization_id, document_id=document_id, processing_job_id=preprocessing_job_id, attempt_number=1, status="completed", schema_version="prep-v1", processor_version="test", original_document_checksum="a" * 64, started_at=now, completed_at=now))
            await session.flush()
            session.add(ProcessingJob(id=analysis_job_id, organization_id=organization_id, document_id=document_id, preprocessing_run_id=preprocessing_run_id, job_type=JobType.DOCUMENT_ANALYSIS.value, status="running", attempt_number=attempt_number))
    return ClaimedJob(job_id=analysis_job_id, organization_id=organization_id, document_id=document_id, job_type=JobType.DOCUMENT_ANALYSIS.value, attempt_number=attempt_number)


def make_runner(dispatcher: JobDispatcher) -> WorkerRunner:
    """Create a runner using the integration database and test dispatcher."""
    return WorkerRunner(
        settings=get_database_settings(), session_factory=get_session_factory(),
        dispatcher=dispatcher,
    )


def test_pipeline_smoke_test_resolves_to_smoke_handler(tmp_path):
    handler = PipelineSmokeTestHandler(get_session_factory(), LocalObjectStorage(tmp_path))
    dispatcher = JobDispatcher({JobType.PIPELINE_SMOKE_TEST: handler})
    assert dispatcher.resolve(JobType.PIPELINE_SMOKE_TEST.value) is handler


@pytest.mark.parametrize("job_type", ["unknown_job", JobType.DOCUMENT_PREPROCESSING.value])
def test_unsupported_or_unregistered_job_type_is_rejected(job_type):
    dispatcher = JobDispatcher({JobType.PIPELINE_SMOKE_TEST: RecordingHandler()})
    with pytest.raises(UnsupportedJobTypeError):
        dispatcher.resolve(job_type)


@pytest.mark.asyncio
async def test_dispatcher_calls_exactly_one_handler():
    selected, other = RecordingHandler(), RecordingHandler()
    dispatcher = JobDispatcher({
        JobType.PIPELINE_SMOKE_TEST: selected,
        JobType.DOCUMENT_PREPROCESSING: other,
    })
    await dispatcher.dispatch(make_context())
    assert len(selected.calls) == 1
    assert other.calls == []


@pytest.mark.asyncio
async def test_successful_handler_causes_runner_completion(tmp_path, organization, pdf_bytes):
    created = await create_job(LocalObjectStorage(tmp_path), organization, pdf_bytes)
    handler = RecordingHandler()
    processed = await make_runner(JobDispatcher({JobType.PIPELINE_SMOKE_TEST: handler})).process_one()
    job = await get_tenant_job(get_session_factory(), organization, created.job_id)
    assert processed is True and len(handler.calls) == 1
    assert job.status == "completed"


@pytest.mark.asyncio
async def test_failed_handler_causes_runner_failure(tmp_path, organization, pdf_bytes):
    created = await create_job(LocalObjectStorage(tmp_path), organization, pdf_bytes)
    handler = RecordingHandler(JobExecutionError("test_failure", "Safe failure."))
    await make_runner(JobDispatcher({JobType.PIPELINE_SMOKE_TEST: handler})).process_one()
    job = await get_tenant_job(get_session_factory(), organization, created.job_id)
    assert (job.status, job.error_code, job.error_message) == (
        "failed", "test_failure", "Safe failure."
    )


@pytest.mark.asyncio
async def test_smoke_handler_cannot_complete_job_independently(tmp_path, organization, pdf_bytes):
    storage = LocalObjectStorage(tmp_path)
    created = await create_job(storage, organization, pdf_bytes)
    claimed = await claim_next_job(get_session_factory(), (JobType.PIPELINE_SMOKE_TEST,))
    await PipelineSmokeTestHandler(get_session_factory(), storage).execute(claimed.to_context())
    job = await get_tenant_job(get_session_factory(), organization, created.job_id)
    assert job.status == "running" and job.completed_at is None


@pytest.mark.asyncio
async def test_live_smoke_behavior_remains_intact(tmp_path, organization, pdf_bytes):
    storage = LocalObjectStorage(tmp_path)
    created = await create_job(storage, organization, pdf_bytes)
    dispatcher = JobDispatcher({
        JobType.PIPELINE_SMOKE_TEST: PipelineSmokeTestHandler(get_session_factory(), storage)
    })
    assert await make_runner(dispatcher).process_one() is True
    job = await get_tenant_job(get_session_factory(), organization, created.job_id)
    assert job.status == "completed"


@pytest.mark.asyncio
async def test_missing_stored_object_is_a_safe_failure(
    tmp_path, organization, pdf_bytes
):
    storage = LocalObjectStorage(tmp_path)
    created = await create_job(storage, organization, pdf_bytes)
    key = f"organizations/{organization}/documents/{created.document_id}.pdf"
    await storage.delete(key)
    dispatcher = JobDispatcher({
        JobType.PIPELINE_SMOKE_TEST: PipelineSmokeTestHandler(get_session_factory(), storage)
    })
    await make_runner(dispatcher).process_one()
    job = await get_tenant_job(get_session_factory(), organization, created.job_id)
    assert (job.status, job.error_code) == ("failed", "stored_object_missing")


@pytest.mark.asyncio
async def test_checksum_mismatch_is_a_safe_failure(
    tmp_path, organization, pdf_bytes
):
    storage = LocalObjectStorage(tmp_path)
    created = await create_job(storage, organization, pdf_bytes)
    key = f"organizations/{organization}/documents/{created.document_id}.pdf"
    await storage.delete(key)
    await storage.put(key, byte_stream(b"different"), maximum_bytes=1024)
    dispatcher = JobDispatcher({
        JobType.PIPELINE_SMOKE_TEST: PipelineSmokeTestHandler(get_session_factory(), storage)
    })
    await make_runner(dispatcher).process_one()
    job = await get_tenant_job(get_session_factory(), organization, created.job_id)
    assert (job.status, job.error_code) == ("failed", "checksum_mismatch")


@pytest.mark.asyncio
async def test_concurrent_claims_do_not_return_same_job(tmp_path, organization, pdf_bytes):
    created = await create_job(LocalObjectStorage(tmp_path), organization, pdf_bytes)
    eligible = (JobType.PIPELINE_SMOKE_TEST,)
    first, second = await asyncio.gather(
        claim_next_job(get_session_factory(), eligible),
        claim_next_job(get_session_factory(), eligible),
    )
    assert [claim.job_id for claim in (first, second) if claim] == [created.job_id]


@pytest.mark.asyncio
async def test_once_processes_at_most_one_job(tmp_path, organization, pdf_bytes):
    storage = LocalObjectStorage(tmp_path)
    first = await create_job(storage, organization, pdf_bytes)
    second = await create_job(storage, organization, pdf_bytes + b"different")
    handler = RecordingHandler()
    exit_code = await make_runner(JobDispatcher({JobType.PIPELINE_SMOKE_TEST: handler})).run(once=True)
    states = [
        (await get_tenant_job(get_session_factory(), organization, job_id)).status
        for job_id in (first.job_id, second.job_id)
    ]
    assert exit_code == 0 and len(handler.calls) == 1
    assert sorted(states) == ["completed", "queued"]


@pytest.mark.asyncio
async def test_once_exits_when_no_job():
    runner = make_runner(JobDispatcher({JobType.PIPELINE_SMOKE_TEST: RecordingHandler()}))
    assert await runner.run(once=True) == 0


@pytest.mark.asyncio
async def test_document_analysis_timeout_gets_exactly_one_automatic_retry(organization):
    """A `document_analysis_timeout` must queue a new attempt without a re-upload.

    Reproduces a real gap: under concurrent load, `document_analysis` calls
    can exceed the fixed wall-clock timeout even though the document itself
    is fine -- a transient failure, not a data or logic defect. Retrying once
    against the same preprocessing run (no re-upload, no redone preprocessing)
    recovers it automatically instead of leaving the document permanently
    failed.

    Drives the runner's retry path directly with a hand-built `ClaimedJob`
    (see `seed_document_analysis_job`) rather than through `process_one`, so
    this never touches `claim_next_job`'s global queue.
    """
    claimed = await seed_document_analysis_job(organization, attempt_number=1)
    runner = make_runner(JobDispatcher({JobType.DOCUMENT_ANALYSIS: RecordingHandler()}))
    await mark_job_failed(get_session_factory(), claimed, error_code="document_analysis_timeout", error_message="Document analysis timed out.")
    assert runner._is_retryable_timeout(claimed, "document_analysis_timeout") is True
    await runner._retry_document_analysis(claimed)

    async with get_session_factory()() as session:
        jobs = (await session.scalars(select(ProcessingJob).where(
            ProcessingJob.organization_id == organization, ProcessingJob.document_id == claimed.document_id,
            ProcessingJob.job_type == JobType.DOCUMENT_ANALYSIS.value,
        ).order_by(ProcessingJob.attempt_number))).all()
    assert [(job.attempt_number, job.status, job.preprocessing_run_id) for job in jobs] == [
        (1, "failed", jobs[0].preprocessing_run_id), (2, "queued", jobs[0].preprocessing_run_id),
    ]


@pytest.mark.asyncio
async def test_document_analysis_timeout_retry_is_capped_at_one(organization):
    """A second consecutive timeout must NOT queue a third attempt.

    `document_analysis_timeout_max_attempts` defaults to 2 (the original
    attempt plus exactly one retry) -- an automatic retry that never stops
    would keep spending money on a document that may simply be too complex
    to finish within any reasonable timeout. Pure decision logic, no DB
    write beyond the fixture seed -- `_is_retryable_timeout` never mutates.
    """
    claimed = await seed_document_analysis_job(organization, attempt_number=2)
    runner = make_runner(JobDispatcher({JobType.DOCUMENT_ANALYSIS: RecordingHandler()}))
    assert runner._is_retryable_timeout(claimed, "document_analysis_timeout") is False


@pytest.mark.asyncio
async def test_non_timeout_document_analysis_failure_is_not_retried(organization):
    """A validation/logic failure must stay single-attempt -- it would just fail identically again."""
    claimed = await seed_document_analysis_job(organization, attempt_number=1)
    runner = make_runner(JobDispatcher({JobType.DOCUMENT_ANALYSIS: RecordingHandler()}))
    assert runner._is_retryable_timeout(claimed, "analysis_validation_failed") is False


@pytest.mark.asyncio
async def test_unregistered_preprocessing_job_is_not_claimed_or_completed(
    tmp_path, organization, pdf_bytes
):
    created = await create_job(LocalObjectStorage(tmp_path), organization, pdf_bytes)
    async with get_session_factory()() as session:
        async with session.begin():
            job = await session.get(ProcessingJob, created.job_id)
            job.job_type = JobType.DOCUMENT_PREPROCESSING.value

    handler = RecordingHandler()
    runner = make_runner(JobDispatcher({JobType.PIPELINE_SMOKE_TEST: handler}))
    assert await runner.run(once=True) == 0
    job = await get_tenant_job(get_session_factory(), organization, created.job_id)
    assert handler.calls == []
    assert job.job_type == JobType.DOCUMENT_PREPROCESSING.value
    assert job.status == "queued"
