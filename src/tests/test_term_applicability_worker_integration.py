"""Focused queue and handler integration checks for term applicability."""
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from maximor.db.models import ProcessingJob, TermApplicabilityRun
from maximor.db.session import get_session_factory
from maximor.jobs.service import schedule_term_applicability_job
from maximor.jobs.types import JobContext, JobType
from maximor.storage import LocalObjectStorage
from maximor.worker.handlers.term_applicability import TermApplicabilityHandler
from test_sku_mapping_worker_integration import _seed


@pytest.mark.asyncio
async def test_term_applicability_schedule_is_idempotent_and_retries_terminal(tmp_path, organization):
    """Only one active job exists, while a terminal job can be retried explicitly."""
    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    first = await schedule_term_applicability_job(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"], analysis_run_id=ids["analysis_run_id"]
    )
    same = await schedule_term_applicability_job(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"], analysis_run_id=ids["analysis_run_id"]
    )
    assert first.id == same.id
    async with get_session_factory()() as session:
        async with session.begin():
            row = await session.get(ProcessingJob, first.id)
            row.status = "failed"
    retry = await schedule_term_applicability_job(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"], analysis_run_id=ids["analysis_run_id"], retry_terminal=True
    )
    assert retry.id != first.id and retry.attempt_number == 2


@pytest.mark.asyncio
async def test_term_applicability_handler_rejects_wrong_job_type(tmp_path, organization):
    """The handler does not own generic job transitions and rejects other types."""
    handler = TermApplicabilityHandler.__new__(TermApplicabilityHandler)
    with pytest.raises(Exception):
        await handler.execute(JobContext(organization, uuid.uuid4(), uuid.uuid4(), JobType.PIPELINE_SMOKE_TEST.value, 1))
