"""Exercise worker routing and status ownership without inspecting PDF content."""

import asyncio
import uuid

import pytest

from maximor.config import get_database_settings
from maximor.db.models import ProcessingJob
from maximor.db.session import get_session_factory
from maximor.jobs.dispatcher import JobDispatcher
from maximor.jobs.errors import JobExecutionError, UnsupportedJobTypeError
from maximor.jobs.service import claim_next_job, create_document_job, get_tenant_job
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
