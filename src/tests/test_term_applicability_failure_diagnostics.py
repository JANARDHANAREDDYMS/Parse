"""Exercise TermApplicabilityHandler's safe failure-stage/code/diagnostics propagation.

All records are generated in the existing PostgreSQL test fixture via
`_seed` (borrowed from `test_sku_mapping_worker_integration`); no supplied
documents, no real Claude call. Fake triage/applicability agents stand in
for `ClaudeTermTriageAgent`/`ClaudeTermApplicabilityAgent`, mirroring the
`_FakeSkuMappingAgent` pattern already established in
`test_sku_mapping_worker_integration.py`.
"""
import uuid
from datetime import UTC, datetime

import pytest

from maximor.config import get_database_settings
from maximor.db.models import TermApplicabilityRun
from maximor.db.session import get_session_factory
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.jobs.dispatcher import JobDispatcher
from maximor.jobs.errors import JobExecutionError
from maximor.jobs.service import get_tenant_job, schedule_term_applicability_job
from maximor.jobs.types import JobContext, JobType
from maximor.storage import LocalObjectStorage
from maximor.term_applicability.agent import TermApplicabilityExecution, TermApplicabilityRuntimeSummary
from maximor.term_applicability.errors import TermApplicabilityRuntimeError, TermApplicabilityValidationError
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
from maximor.term_applicability.schemas import TermApplicabilityDecision, TermApplicabilityResult, TermDisposition
from maximor.term_applicability.versions import TERM_APPLICABILITY_RESULT_SCHEMA_VERSION
from maximor.term_triage.agent import TermTriageExecution, TermTriageRuntimeSummary
from maximor.term_triage.errors import TermTriageRuntimeError
from maximor.term_triage.schemas import TermTriageResult
from maximor.term_triage.versions import TERM_TRIAGE_RESULT_SCHEMA_VERSION
from maximor.worker.handlers.term_applicability import TermApplicabilityHandler
from maximor.worker.runner import WorkerRunner
from test_sku_mapping_worker_integration import _seed


# --- fake agents -------------------------------------------------------------------

class _FakeTermTriageAgent:
    """Return a fixed successful execution, or raise a configured typed error."""

    def __init__(self, *, error: Exception | None = None, runtime: TermTriageRuntimeSummary | None = None) -> None:
        self._error = error
        self._runtime = runtime or TermTriageRuntimeSummary(
            request_id=uuid.uuid4(), started_at=datetime.now(UTC),
            terminal_reason="accepted_by_finalizer", terminal_kind="accepted_by_finalizer",
            correction_attempt_count=0, finalization_accepted=True,
        )

    async def execute(self, task) -> TermTriageExecution:
        if self._error is not None:
            raise self._error
        result = TermTriageResult(
            schema_version=TERM_TRIAGE_RESULT_SCHEMA_VERSION,
            organization_id=task.organization_id, document_id=task.document_id,
            preprocessing_run_id=task.preprocessing_run_id, analysis_run_id=task.analysis_run_id,
            decisions=(),
        )
        return TermTriageExecution(result=result, runtime=self._runtime)


class _FakeTermApplicabilityAgent:
    """Return a fixed (optionally invalid) execution, or raise a configured typed error."""

    def __init__(self, *, error: Exception | None = None, runtime: TermApplicabilityRuntimeSummary | None = None, invalid: bool = False) -> None:
        self._error, self._invalid = error, invalid
        self._runtime = runtime or TermApplicabilityRuntimeSummary(
            request_id=uuid.uuid4(), started_at=datetime.now(UTC),
            terminal_reason="accepted_by_finalizer", terminal_kind="accepted_by_finalizer",
            correction_attempt_count=0, finalization_accepted=True,
        )

    async def execute(self, task, tools) -> TermApplicabilityExecution:
        if self._error is not None:
            raise self._error
        decisions = ()
        if self._invalid:
            # `task` has zero global terms (see `_seed`), so naming any term_id
            # at all is trivially an `unknown_term` -- a deterministic, minimal
            # way to fail `validate_term_applicability_result` without needing
            # real evidence/candidate machinery.
            decisions = (TermApplicabilityDecision(schema_version="1.0.0", term_id="term-ghost", disposition=TermDisposition.DOCUMENT_METADATA),)
        result = TermApplicabilityResult(
            schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION,
            organization_id=task.organization_id, document_id=task.document_id,
            preprocessing_run_id=task.preprocessing_run_id, analysis_run_id=task.analysis_run_id,
            decisions=decisions, candidate_commercial_facts=(), candidate_commercial_fact_coverage=(),
        )
        return TermApplicabilityExecution(result=result, runtime=self._runtime)


class _SaveFailingEnrichment:
    """Delegate everything except `save_completed_result`, which always fails."""

    def __init__(self, real: TermApplicabilityPersistenceService) -> None:
        self._real = real

    async def create_run(self, **kwargs): return await self._real.create_run(**kwargs)
    async def save_completed_result(self, **kwargs): raise ValueError("term applicability artifact too large")
    async def load_completed_result(self, **kwargs): return await self._real.load_completed_result(**kwargs)
    async def mark_run_failed(self, *args, **kwargs): return await self._real.mark_run_failed(*args, **kwargs)


class _ReloadFailingEnrichment:
    """Delegate everything except `load_completed_result`, which always fails."""

    def __init__(self, real: TermApplicabilityPersistenceService) -> None:
        self._real = real

    async def create_run(self, **kwargs): return await self._real.create_run(**kwargs)
    async def save_completed_result(self, **kwargs): return await self._real.save_completed_result(**kwargs)
    async def load_completed_result(self, **kwargs): raise ValueError("term applicability result unavailable")
    async def mark_run_failed(self, *args, **kwargs): return await self._real.mark_run_failed(*args, **kwargs)


# --- shared setup --------------------------------------------------------------------

async def _context_for(organization: uuid.UUID, ids: dict):
    """Schedule one real term_applicability job and build its dispatch context."""

    job = await schedule_term_applicability_job(
        get_session_factory(), organization_id=organization, document_id=ids["document_id"], analysis_run_id=ids["analysis_run_id"],
    )
    context = JobContext(organization_id=organization, document_id=ids["document_id"], processing_job_id=job.id, job_type=JobType.TERM_APPLICABILITY.value, attempt_number=1)
    return context, job


async def _run_id_for(job) -> uuid.UUID:
    return uuid.uuid5(job.id, "1")


async def _load_run(run_id: uuid.UUID) -> TermApplicabilityRun:
    async with get_session_factory()() as session:
        return await session.get(TermApplicabilityRun, run_id)


def _handler(storage, enrichment_results, *, triage_agent, applicability_agent, analysis_results=None) -> TermApplicabilityHandler:
    return TermApplicabilityHandler(
        get_database_settings(), get_session_factory(), storage,
        analysis_results or DocumentAnalysisPersistenceService(get_session_factory(), storage),
        enrichment_results, triage_agent=triage_agent, applicability_agent=applicability_agent,
    )


# --- triage failures -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_triage_timeout_preserves_code_stage_and_partial_runtime(tmp_path, organization):
    """A typed triage timeout is preserved exactly, tagged with the triage stage."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    storage = LocalObjectStorage(tmp_path)
    enrichment_results = TermApplicabilityPersistenceService(get_session_factory(), storage)
    partial_runtime = TermTriageRuntimeSummary(
        request_id=uuid.uuid4(), started_at=datetime.now(UTC),
        terminal_reason="timeout", terminal_kind="timeout", correction_attempt_count=0,
        total_elapsed_ms=60_000, session_initialization_elapsed_ms=1_200,
    )
    handler = _handler(
        storage, enrichment_results,
        triage_agent=_FakeTermTriageAgent(error=TermTriageRuntimeError("term_triage_timeout", runtime=partial_runtime)),
        applicability_agent=_FakeTermApplicabilityAgent(),
    )
    context, job = await _context_for(organization, ids)

    with pytest.raises(JobExecutionError) as excinfo:
        await handler.execute(context)
    assert excinfo.value.code == "term_triage_timeout"

    row = await _load_run(await _run_id_for(job))
    assert row.status == "failed"
    assert row.error_code == "term_triage_timeout"
    assert row.completed_at is not None  # previously always left null on failure
    diagnostics = row.runtime_diagnostics
    assert diagnostics["failure_stage"] == "triage"
    assert diagnostics["applicability"] is None  # applicability never ran
    triage_block = diagnostics["triage"]
    assert triage_block["diagnostics"]["terminal_kind"] == "timeout"
    assert triage_block["diagnostics"]["total_elapsed_ms"] == 60_000
    assert triage_block["diagnostics"]["session_initialization_elapsed_ms"] == 1_200


# --- applicability failures ------------------------------------------------------------

@pytest.mark.asyncio
async def test_applicability_timeout_preserves_code_stage_and_both_runtimes(tmp_path, organization):
    """A typed applicability timeout is preserved, and triage's own success diagnostics survive alongside it."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    storage = LocalObjectStorage(tmp_path)
    enrichment_results = TermApplicabilityPersistenceService(get_session_factory(), storage)
    partial_runtime = TermApplicabilityRuntimeSummary(
        request_id=uuid.uuid4(), started_at=datetime.now(UTC),
        terminal_reason="timeout", terminal_kind="timeout", correction_attempt_count=1,
        finalization_accepted=False, finalization_submission_count=1,
        successful_tool_call_order=("get_term_evidence_region", "get_candidate_evidence_region"),
        tool_call_count=2, mcp_server_status="connected", total_elapsed_ms=150_000,
        session_initialization_elapsed_ms=900,
    )
    handler = _handler(
        storage, enrichment_results,
        triage_agent=_FakeTermTriageAgent(),
        applicability_agent=_FakeTermApplicabilityAgent(error=TermApplicabilityRuntimeError("term_applicability_timeout", runtime=partial_runtime)),
    )
    context, job = await _context_for(organization, ids)

    with pytest.raises(JobExecutionError) as excinfo:
        await handler.execute(context)
    assert excinfo.value.code == "term_applicability_timeout"

    row = await _load_run(await _run_id_for(job))
    assert row.status == "failed" and row.error_code == "term_applicability_timeout" and row.completed_at is not None
    diagnostics = row.runtime_diagnostics
    assert diagnostics["failure_stage"] == "applicability"
    assert diagnostics["triage"] is not None  # triage succeeded before applicability failed
    assert diagnostics["triage"]["diagnostics"]["finalization_accepted"] is True
    app_block = diagnostics["applicability"]
    assert app_block["diagnostics"]["terminal_kind"] == "timeout"
    assert app_block["diagnostics"]["correction_attempt_count"] == 1
    assert app_block["diagnostics"]["finalization_accepted"] is False
    assert app_block["diagnostics"]["finalization_submission_count"] == 1
    assert app_block["diagnostics"]["successful_tool_call_order"] == ["get_term_evidence_region", "get_candidate_evidence_region"]
    assert app_block["diagnostics"]["total_elapsed_ms"] == 150_000
    assert app_block["diagnostics"]["session_initialization_elapsed_ms"] == 900


@pytest.mark.asyncio
async def test_applicability_validation_failure_reports_validation_stage(tmp_path, organization):
    """A structurally valid but rule-violating result is a `validation`-stage failure, not `applicability`."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    storage = LocalObjectStorage(tmp_path)
    enrichment_results = TermApplicabilityPersistenceService(get_session_factory(), storage)
    handler = _handler(
        storage, enrichment_results,
        triage_agent=_FakeTermTriageAgent(),
        applicability_agent=_FakeTermApplicabilityAgent(invalid=True),
    )
    context, job = await _context_for(organization, ids)

    with pytest.raises(JobExecutionError) as excinfo:
        await handler.execute(context)
    assert excinfo.value.code == "term_applicability_validation_failed"

    row = await _load_run(await _run_id_for(job))
    assert row.status == "failed" and row.error_code == "term_applicability_validation_failed"
    assert row.runtime_diagnostics["failure_stage"] == "validation"
    assert row.runtime_diagnostics["applicability"] is not None


@pytest.mark.asyncio
async def test_applicability_sdk_transport_failure_with_no_runtime(tmp_path, organization):
    """A raw SDK/transport failure with no captured runtime still preserves its exact code and stage."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    storage = LocalObjectStorage(tmp_path)
    enrichment_results = TermApplicabilityPersistenceService(get_session_factory(), storage)
    handler = _handler(
        storage, enrichment_results,
        triage_agent=_FakeTermTriageAgent(),
        applicability_agent=_FakeTermApplicabilityAgent(error=TermApplicabilityRuntimeError("term_applicability_sdk_transport_failed", runtime=None)),
    )
    context, job = await _context_for(organization, ids)

    with pytest.raises(JobExecutionError) as excinfo:
        await handler.execute(context)
    assert excinfo.value.code == "term_applicability_sdk_transport_failed"

    row = await _load_run(await _run_id_for(job))
    assert row.error_code == "term_applicability_sdk_transport_failed"
    assert row.runtime_diagnostics["failure_stage"] == "applicability"
    assert row.runtime_diagnostics["applicability"] is None  # no runtime was ever returned
    assert row.runtime_diagnostics["triage"] is not None


@pytest.mark.asyncio
async def test_applicability_evidence_coverage_style_validation_error_preserves_runtime(tmp_path, organization):
    """`TermApplicabilityValidationError` raised by the agent itself is also stage=applicability, code preserved."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    storage = LocalObjectStorage(tmp_path)
    enrichment_results = TermApplicabilityPersistenceService(get_session_factory(), storage)
    runtime = TermApplicabilityRuntimeSummary(
        request_id=uuid.uuid4(), started_at=datetime.now(UTC),
        terminal_reason="validation_failure", terminal_kind="validation_failure",
        validation_issue_codes=("fact_evidence_not_retrieved",),
    )
    handler = _handler(
        storage, enrichment_results,
        triage_agent=_FakeTermTriageAgent(),
        applicability_agent=_FakeTermApplicabilityAgent(error=TermApplicabilityValidationError(runtime=runtime)),
    )
    context, job = await _context_for(organization, ids)

    with pytest.raises(JobExecutionError) as excinfo:
        await handler.execute(context)
    assert excinfo.value.code == "term_applicability_invalid_output"

    row = await _load_run(await _run_id_for(job))
    assert row.runtime_diagnostics["failure_stage"] == "applicability"
    assert row.runtime_diagnostics["applicability"]["diagnostics"]["validation_issue_codes"] == ["fact_evidence_not_retrieved"]


# --- persistence / reload-validation failures -------------------------------------------

@pytest.mark.asyncio
async def test_persistence_failure_is_tagged_persistence_stage(tmp_path, organization):
    """A canonical-artifact save failure is a `persistence`-stage failure with both runtimes kept."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    storage = LocalObjectStorage(tmp_path)
    real = TermApplicabilityPersistenceService(get_session_factory(), storage)
    handler = _handler(
        storage, _SaveFailingEnrichment(real),
        triage_agent=_FakeTermTriageAgent(), applicability_agent=_FakeTermApplicabilityAgent(),
    )
    context, job = await _context_for(organization, ids)

    with pytest.raises(JobExecutionError) as excinfo:
        await handler.execute(context)
    assert excinfo.value.code == "term_applicability_persistence_failed"

    row = await _load_run(await _run_id_for(job))
    assert row.status == "failed"
    assert row.runtime_diagnostics["failure_stage"] == "persistence"
    assert row.runtime_diagnostics["triage"] is not None
    assert row.runtime_diagnostics["applicability"] is not None


@pytest.mark.asyncio
async def test_reload_validation_failure_never_overwrites_an_already_completed_run(tmp_path, organization):
    """A reload failure after a genuine successful save reports its own code, without corrupting the saved artifact.

    Mirrors `mark_run_failed`'s (and its `document_analysis`/`sku_mapping`
    siblings') "never overwrite completion" guarantee: the artifact really
    was saved, so the domain run correctly stays `completed` even though the
    job itself still fails because the handler could not re-confirm it.
    """

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    storage = LocalObjectStorage(tmp_path)
    real = TermApplicabilityPersistenceService(get_session_factory(), storage)
    handler = _handler(
        storage, _ReloadFailingEnrichment(real),
        triage_agent=_FakeTermTriageAgent(), applicability_agent=_FakeTermApplicabilityAgent(),
    )
    context, job = await _context_for(organization, ids)

    with pytest.raises(JobExecutionError) as excinfo:
        await handler.execute(context)
    assert excinfo.value.code == "term_applicability_reload_unavailable"

    row = await _load_run(await _run_id_for(job))
    assert row.status == "completed"  # the real save genuinely succeeded and was not overwritten
    assert row.canonical_result_storage_key is not None


# --- runner ownership of generic job status ------------------------------------------

@pytest.mark.asyncio
async def test_runner_marks_generic_job_failed_with_the_exact_typed_code(tmp_path, organization):
    """WorkerRunner, not the handler, owns `processing_jobs`, and receives the exact safe code."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    storage = LocalObjectStorage(tmp_path)
    enrichment_results = TermApplicabilityPersistenceService(get_session_factory(), storage)
    handler = _handler(
        storage, enrichment_results,
        triage_agent=_FakeTermTriageAgent(),
        applicability_agent=_FakeTermApplicabilityAgent(error=TermApplicabilityRuntimeError("term_applicability_authentication_failed", runtime=None)),
    )
    context, job = await _context_for(organization, ids)
    runner = WorkerRunner(
        settings=get_database_settings(), session_factory=get_session_factory(),
        dispatcher=JobDispatcher({JobType.TERM_APPLICABILITY: handler}),
    )

    processed = await runner.process_one()
    assert processed is True

    tenant_job = await get_tenant_job(get_session_factory(), organization, job.id)
    assert tenant_job.status == "failed"
    assert tenant_job.error_code == "term_applicability_authentication_failed"  # never the generic "worker_operation_failed"


# --- API exposure ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_exposes_safe_failed_status_stage_and_diagnostics(tmp_path, organization, app_client):
    """The GET endpoint returns the persisted safe error code, stage, and diagnostics for a failed run."""

    _, client = app_client
    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    storage = LocalObjectStorage(tmp_path)
    enrichment_results = TermApplicabilityPersistenceService(get_session_factory(), storage)
    handler = _handler(
        storage, enrichment_results,
        triage_agent=_FakeTermTriageAgent(),
        applicability_agent=_FakeTermApplicabilityAgent(error=TermApplicabilityRuntimeError("term_applicability_timeout", runtime=None)),
    )
    context, _job = await _context_for(organization, ids)
    with pytest.raises(JobExecutionError):
        await handler.execute(context)

    response = await client.get(f"/v1/organizations/{organization}/documents/{ids['document_id']}/term-applicability")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "term_applicability_timeout"
    assert body["failure_stage"] == "applicability"
    assert body["result"] is None
    assert isinstance(body["diagnostics"], dict) and body["diagnostics"]["failure_stage"] == "applicability"


# --- sanitization -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failure_diagnostics_and_logs_exclude_arbitrary_exception_text(tmp_path, organization):
    """An unexpected exception's own message text never reaches persisted diagnostics or logs."""

    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    storage = LocalObjectStorage(tmp_path)
    enrichment_results = TermApplicabilityPersistenceService(get_session_factory(), storage)

    class _Boom(_FakeTermApplicabilityAgent):
        async def execute(self, task, tools):
            raise RuntimeError("secret-looking-database-url postgres://user:hunter2@host/db")

    handler = _handler(
        storage, enrichment_results,
        triage_agent=_FakeTermTriageAgent(), applicability_agent=_Boom(),
    )
    context, job = await _context_for(organization, ids)

    with pytest.raises(JobExecutionError) as excinfo:
        await handler.execute(context)
    assert excinfo.value.code == "term_applicability_failed"  # generic, unexpected-stage fallback
    assert "hunter2" not in excinfo.value.safe_message

    row = await _load_run(await _run_id_for(job))
    # The unhandled RuntimeError surfaced while `stage` was already
    # "applicability" (set just before the agent's own `execute()` call), so
    # that -- the actually-accurate phase -- is what gets recorded, not the
    # coarser "unexpected" default that only applies before any stage is set.
    assert row.runtime_diagnostics["failure_stage"] == "applicability"
    blob = str(row.runtime_diagnostics) + (row.error_message or "")
    assert "hunter2" not in blob and "postgres://" not in blob
