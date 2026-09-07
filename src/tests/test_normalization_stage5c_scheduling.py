"""Database-backed tests for `schedule_normalization_if_ready` fan-in scheduling.

No supplied PDFs, worker/API processes, or paid Claude calls anywhere in
this file -- everything is generated via the existing `_seed` fixture and
direct `ProcessingJob`/`TermApplicabilityRun` rows, mirroring
`test_sku_mapping_worker_integration.py`'s own style.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from maximor.db.models import ProcessingJob, TermApplicabilityRun
from maximor.db.session import get_session_factory
from maximor.jobs.normalization import schedule_normalization_if_ready
from maximor.jobs.types import JobType
from maximor.storage import LocalObjectStorage
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
from maximor.term_applicability.schemas import TermApplicabilityResult
from maximor.term_triage.schemas import TermTriageResult
from test_sku_mapping_worker_integration import _seed


async def _term_job(organization_id: uuid.UUID, ids: dict, *, status: str) -> ProcessingJob:
    """Seed one `term_applicability` processing job row at a given status."""

    async with get_session_factory()() as session:
        async with session.begin():
            job = ProcessingJob(
                id=uuid.uuid4(), organization_id=organization_id, document_id=ids["document_id"],
                analysis_run_id=ids["analysis_run_id"], job_type=JobType.TERM_APPLICABILITY.value,
                status=status, attempt_number=1,
            )
            session.add(job)
        return job


async def _term_run(organization_id: uuid.UUID, ids: dict, storage: LocalObjectStorage, *, status: str) -> TermApplicabilityRun:
    """Seed one `term_applicability_runs` row, optionally completing it."""

    service = TermApplicabilityPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization_id, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1, schema_version="v",
        prompt_version="p", skill_version="s", agent_version="a", model="fake", started_at=datetime.now(UTC),
    )
    if status == "completed":
        triage = TermTriageResult(
            schema_version="v", organization_id=organization_id, document_id=ids["document_id"],
            preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"], decisions=(),
        )
        applicability = TermApplicabilityResult(
            schema_version="v", organization_id=organization_id, document_id=ids["document_id"],
            preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
            decisions=(), candidate_commercial_facts=(), candidate_commercial_fact_coverage=(),
        )
        await service.save_completed_result(run_id=run_id, triage=triage, applicability=applicability)
    elif status == "failed":
        await service.mark_run_failed(run_id, error_code="term_applicability_failed", error_message="safe failure")
    async with get_session_factory()() as session:
        return await session.get(TermApplicabilityRun, run_id)


async def _sku_job(organization_id: uuid.UUID, ids: dict, candidate_name: str, *, status: str) -> ProcessingJob:
    """Seed one `sku_mapping` processing job row for one seeded candidate at a given status."""

    async with get_session_factory()() as session:
        async with session.begin():
            job = ProcessingJob(
                id=uuid.uuid4(), organization_id=organization_id, document_id=ids["document_id"],
                analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=ids["candidate_ids"][candidate_name],
                job_type=JobType.SKU_MAPPING.value, status=status, attempt_number=1,
            )
            session.add(job)
        return job


@pytest.mark.asyncio
async def test_schedules_normalization_once_every_branch_is_terminal(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _term_job(organization, ids, status="completed")
    term_run = await _term_run(organization, ids, storage, status="completed")
    await _sku_job(organization, ids, "candidate-purchased", status="completed")

    job = await schedule_normalization_if_ready(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"],
    )
    assert job is not None
    assert job.job_type == JobType.NORMALIZATION.value
    assert job.status == "queued"
    assert job.analysis_run_id == ids["analysis_run_id"]
    del term_run


@pytest.mark.asyncio
async def test_does_not_schedule_while_the_term_applicability_job_is_active(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _term_job(organization, ids, status="running")
    await _term_run(organization, ids, storage, status="running")
    await _sku_job(organization, ids, "candidate-purchased", status="completed")

    job = await schedule_normalization_if_ready(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"],
    )
    assert job is None


@pytest.mark.asyncio
async def test_does_not_schedule_while_a_sku_mapping_job_is_active(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _term_job(organization, ids, status="completed")
    await _term_run(organization, ids, storage, status="completed")
    await _sku_job(organization, ids, "candidate-purchased", status="running")

    job = await schedule_normalization_if_ready(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"],
    )
    assert job is None


@pytest.mark.asyncio
async def test_does_not_schedule_while_an_eligible_candidate_has_no_sku_mapping_job_yet(tmp_path, organization):
    """Regression: the fan-in check must read expected eligible candidates from
    the persisted document-analysis result, not merely check that whichever
    sku_mapping jobs already exist are terminal.

    `_seed` creates exactly one eligible (`purchased`) candidate
    (`candidate-purchased`) among three total. If no sku_mapping job has
    been scheduled for it at all -- e.g. `DocumentAnalysisHandler` is still
    mid-loop -- normalization must not be scheduled just because the term
    job is done and zero sku_mapping jobs happen to be active.
    """

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _term_job(organization, ids, status="completed")
    await _term_run(organization, ids, storage, status="completed")
    # Deliberately do not seed any sku_mapping job for candidate-purchased.

    job = await schedule_normalization_if_ready(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"],
    )
    assert job is None


@pytest.mark.asyncio
async def test_schedules_normalization_when_the_term_applicability_branch_failed(tmp_path, organization):
    """A durable technical failure on a required branch must not strand the document.

    The scheduler treats a `failed` term-applicability job the same as a
    `completed` one for fan-in purposes (both are terminal) -- it is
    `NormalizationHandler`'s job to turn the still-uncompleted
    `TermApplicabilityRun` into an honest `FAILED_VALIDATION` domain result,
    not the scheduler's job to withhold scheduling forever.
    """

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _term_job(organization, ids, status="failed")
    term_run = await _term_run(organization, ids, storage, status="failed")
    await _sku_job(organization, ids, "candidate-purchased", status="completed")

    job = await schedule_normalization_if_ready(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"],
    )
    assert job is not None
    assert term_run.status == "failed"


@pytest.mark.asyncio
async def test_concurrent_calls_never_create_duplicate_active_normalization_jobs(tmp_path, organization):
    """Multiple SKU handlers and the term handler finishing concurrently must
    never race into two active normalization jobs -- the partial unique
    index on `processing_jobs` is the actual guarantee; this exercises it
    end to end through the service's own `IntegrityError` recovery path."""

    import asyncio

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _term_job(organization, ids, status="completed")
    await _term_run(organization, ids, storage, status="completed")
    await _sku_job(organization, ids, "candidate-purchased", status="completed")

    results = await asyncio.gather(*(
        schedule_normalization_if_ready(
            get_session_factory(), organization_id=organization, document_id=ids["document_id"],
            analysis_run_id=ids["analysis_run_id"],
        )
        for _ in range(5)
    ))
    assert all(result is not None for result in results)
    assert len({result.id for result in results}) == 1

    async with get_session_factory()() as session:
        active = (await session.scalars(
            select(ProcessingJob).where(
                ProcessingJob.analysis_run_id == ids["analysis_run_id"],
                ProcessingJob.job_type == JobType.NORMALIZATION.value,
                ProcessingJob.status.in_(["queued", "running"]),
            )
        )).all()
    assert len(active) == 1
