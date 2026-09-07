"""Regression tests: a durable branch failure must still trigger normalization fan-in.

Live-pipeline regression discovered on a real of-0001.pdf run: `TermApplicabilityHandler`
timed out after every `sku_mapping` job had already completed. Because
`schedule_normalization_if_ready` was only ever called from each handler's
*success* path, and the sku_mapping success-path check ran while
term_applicability was still `running` (so it correctly deferred), nothing
ever re-checked the fan-in condition once term_applicability later failed --
the document was permanently stuck with no `normalization` job ever created.

No supplied PDFs, no paid Claude call -- every agent here is an in-memory
fake that raises immediately.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from maximor.config import get_database_settings
from maximor.db.models import ProcessingJob
from maximor.db.session import get_session_factory
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.jobs.errors import JobExecutionError
from maximor.jobs.service import schedule_sku_mapping_job
from maximor.jobs.types import JobContext, JobType
from maximor.sku_mapping.errors import SkuMappingError
from maximor.sku_mapping.persistence import SkuMappingPersistenceService
from maximor.storage import LocalObjectStorage
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
from maximor.term_applicability.schemas import TermApplicabilityResult
from maximor.term_triage.errors import TermTriageError
from maximor.term_triage.schemas import TermTriageResult
from maximor.worker.handlers.sku_mapping import SkuMappingHandler
from maximor.worker.handlers.term_applicability import TermApplicabilityHandler
from test_sku_mapping_worker_integration import _seed


class _FailingSkuMappingAgent:
    """Simulate a genuine SKU-mapping agent/runtime failure."""

    async def execute(self, task, tools):
        raise SkuMappingError("sku_mapping_timeout", "SKU mapping could not be completed.")


class _FailingTriageAgent:
    """Simulate a genuine term-triage agent/runtime failure -- the exact live failure mode."""

    async def execute(self, task):
        raise TermTriageError("term_triage_timeout", "Term triage could not be completed.")


async def _completed_term_applicability_run(ids: dict, storage: LocalObjectStorage, organization_id: uuid.UUID) -> None:
    """Persist one completed, empty combined term-applicability run and its job row."""

    service = TermApplicabilityPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    async with get_session_factory()() as session:
        async with session.begin():
            session.add(ProcessingJob(
                id=uuid.uuid4(), organization_id=organization_id, document_id=ids["document_id"],
                analysis_run_id=ids["analysis_run_id"], job_type=JobType.TERM_APPLICABILITY.value,
                status="completed", attempt_number=1,
            ))
    await service.create_run(
        run_id=run_id, organization_id=organization_id, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1, schema_version="v",
        prompt_version="p", skill_version="s", agent_version="a", model="fake", started_at=datetime.now(UTC),
    )
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


async def _normalization_job_exists(organization_id: uuid.UUID, analysis_run_id: uuid.UUID) -> bool:
    async with get_session_factory()() as session:
        job = await session.scalar(select(ProcessingJob).where(
            ProcessingJob.organization_id == organization_id,
            ProcessingJob.analysis_run_id == analysis_run_id,
            ProcessingJob.job_type == JobType.NORMALIZATION.value,
        ))
    return job is not None


@pytest.mark.asyncio
async def test_sku_mapping_handler_failure_still_schedules_normalization_when_term_applicability_already_completed(tmp_path, organization):
    """The exact scenario this regression covers, from the sku_mapping side.

    `_seed` creates exactly one eligible (`purchased`) candidate --
    `candidate-purchased` -- so this failing sku_mapping job is the only
    required branch besides term_applicability, which is already completed.
    """
    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _completed_term_applicability_run(ids, storage, organization)

    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    mapping_results = SkuMappingPersistenceService(get_session_factory(), storage)
    candidate_id = ids["candidate_ids"]["candidate-purchased"]
    job = await schedule_sku_mapping_job(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=candidate_id,
    )
    handler = SkuMappingHandler(get_database_settings(), get_session_factory(), storage, analysis_results, mapping_results, agent=_FailingSkuMappingAgent())
    context = JobContext(organization_id=organization, document_id=ids["document_id"], processing_job_id=job.id, job_type=JobType.SKU_MAPPING.value, attempt_number=1)

    with pytest.raises(JobExecutionError) as excinfo:
        await handler.execute(context)
    assert excinfo.value.code == "sku_mapping_timeout"

    assert await _normalization_job_exists(organization, ids["analysis_run_id"])


@pytest.mark.asyncio
async def test_term_applicability_handler_failure_still_schedules_normalization_when_sku_mapping_already_completed(tmp_path, organization):
    """The exact live-pipeline regression: term_applicability times out after every
    sku_mapping job has already completed successfully."""

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)

    # Mark the sole eligible candidate's sku_mapping job completed directly --
    # this test is only about term_applicability's own failure path, not
    # sku_mapping's own execution.
    async with get_session_factory()() as session:
        async with session.begin():
            session.add(ProcessingJob(
                id=uuid.uuid4(), organization_id=organization, document_id=ids["document_id"],
                analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=ids["candidate_ids"]["candidate-purchased"],
                job_type=JobType.SKU_MAPPING.value, status="completed", attempt_number=1,
            ))
            job = ProcessingJob(
                id=uuid.uuid4(), organization_id=organization, document_id=ids["document_id"],
                analysis_run_id=ids["analysis_run_id"], job_type=JobType.TERM_APPLICABILITY.value,
                status="running", attempt_number=1,
            )
            session.add(job)

    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    enrichment_results = TermApplicabilityPersistenceService(get_session_factory(), storage)
    handler = TermApplicabilityHandler(
        get_database_settings(), get_session_factory(), storage, analysis_results, enrichment_results,
        triage_agent=_FailingTriageAgent(),
    )
    context = JobContext(organization_id=organization, document_id=ids["document_id"], processing_job_id=job.id, job_type=JobType.TERM_APPLICABILITY.value, attempt_number=1)

    with pytest.raises(JobExecutionError) as excinfo:
        await handler.execute(context)
    assert excinfo.value.code == "term_triage_timeout"

    assert await _normalization_job_exists(organization, ids["analysis_run_id"])


@pytest.mark.asyncio
async def test_sku_mapping_handler_failure_does_not_schedule_normalization_while_term_applicability_still_running(tmp_path, organization):
    """A failure must not force scheduling while a sibling required branch is still active."""

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    async with get_session_factory()() as session:
        async with session.begin():
            session.add(ProcessingJob(
                id=uuid.uuid4(), organization_id=organization, document_id=ids["document_id"],
                analysis_run_id=ids["analysis_run_id"], job_type=JobType.TERM_APPLICABILITY.value,
                status="running", attempt_number=1,
            ))

    analysis_results = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    mapping_results = SkuMappingPersistenceService(get_session_factory(), storage)
    candidate_id = ids["candidate_ids"]["candidate-purchased"]
    job = await schedule_sku_mapping_job(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], document_product_candidate_id=candidate_id,
    )
    handler = SkuMappingHandler(get_database_settings(), get_session_factory(), storage, analysis_results, mapping_results, agent=_FailingSkuMappingAgent())
    context = JobContext(organization_id=organization, document_id=ids["document_id"], processing_job_id=job.id, job_type=JobType.SKU_MAPPING.value, attempt_number=1)

    with pytest.raises(JobExecutionError):
        await handler.execute(context)

    assert not await _normalization_job_exists(organization, ids["analysis_run_id"])
