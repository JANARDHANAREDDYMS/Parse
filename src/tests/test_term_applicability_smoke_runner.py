"""Test the two-stage smoke wrapper with generated fake agents only."""

import uuid
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from maximor.document_analysis.schemas import CommercialStatus, CommercialStatusAssessment, DocumentAnalysisResult, GlobalTerm, ProductCandidate
from maximor.term_applicability.contracts import build_term_applicability_task
from maximor.term_applicability.agent import TermApplicabilityRuntimeSummary
from maximor.term_applicability.errors import TermApplicabilityRuntimeError
from maximor.term_applicability.versions import TERM_APPLICABILITY_RESULT_SCHEMA_VERSION
from maximor.term_applicability.smoke_runner import diagnostics_directory, run_two_stage_smoke
from maximor.term_triage.schemas import TermTriageDecision, TermTriageDisposition, TermTriageResult


def _result():
    """Build a minimal generated analysis result for orchestration tests."""

    run = uuid.uuid4()
    org, doc = uuid.uuid4(), uuid.uuid4()
    return DocumentAnalysisResult(
        schema_version="analysis-v1", organization_id=org, document_id=doc, preprocessing_run_id=run,
        preprocessing_schema_version="prep-v1", prompt_version="prompt-v1", agent_version="agent-v1",
        product_candidates=(ProductCandidate(candidate_id="candidate-0001", raw_name="Service"),),
        commercial_statuses=(CommercialStatusAssessment(assessment_id="status-0001", status=CommercialStatus.PURCHASED, candidate_id="candidate-0001"),),
        global_terms=(GlobalTerm(term_id="term-0001", raw_name="Payment", raw_value="Net 30"),),
    )


class FakeTriage:
    """Return a validated triage result and retain the received task."""

    def __init__(self): self.task = None
    async def triage(self, task):
        self.task = task
        return TermTriageResult(schema_version="1.0.0", organization_id=task.organization_id, document_id=task.document_id, preprocessing_run_id=task.preprocessing_run_id, analysis_run_id=task.analysis_run_id, decisions=(TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.POTENTIAL_LINE_ITEM),))


class FakeApplicability:
    """Capture the correctly typed selected applicability task."""

    def __init__(self, fail=False): self.task = None; self.fail = fail
    async def resolve(self, task, tools):
        self.task = task
        if self.fail: raise RuntimeError("generated failure")
        return __import__("maximor.term_applicability.schemas", fromlist=["TermApplicabilityResult"]).TermApplicabilityResult(schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, organization_id=task.organization_id, document_id=task.document_id, preprocessing_run_id=task.preprocessing_run_id, analysis_run_id=task.analysis_run_id, decisions=())


class FailingApplicability:
    """Raise a typed timeout carrying bounded partial runtime facts."""

    def __init__(self, runtime): self.runtime = runtime
    async def resolve(self, task, tools):
        del task, tools
        raise TermApplicabilityRuntimeError("term_applicability_timeout", runtime=self.runtime)


class MismatchedTriage(FakeTriage):
    """Return a structurally valid result for a different trusted run."""

    async def triage(self, task):
        self.task = task
        return TermTriageResult(
            schema_version="1.0.0",
            organization_id=task.organization_id,
            document_id=task.document_id,
            preprocessing_run_id=task.preprocessing_run_id,
            analysis_run_id=uuid.uuid4(),
            decisions=(TermTriageDecision(term_id="term-0001", disposition=TermTriageDisposition.POTENTIAL_LINE_ITEM),),
        )


@pytest.mark.asyncio
async def test_wrapper_builds_full_applicability_task_and_writes_triage_first(tmp_path: Path):
    """The selected-task factory receives a full applicability task, not triage."""

    result = _result(); triage = FakeTriage(); applicability = FakeApplicability()
    output = await run_two_stage_smoke(result, analysis_run_id=uuid.uuid4(), triage_agent=triage, applicability_agent=applicability, applicability_tools=object(), output_dir=tmp_path)
    assert triage.task is not None and applicability.task is not None
    assert applicability.task.analysis_run_id == triage.task.analysis_run_id
    assert applicability.task.candidates[0].raw_name == result.product_candidates[0].raw_name
    assert applicability.task.terms[0].raw_value == result.global_terms[0].raw_value
    assert (output / "term-triage-result.json").exists()
    assert (output / "term-applicability-result.json").exists()


@pytest.mark.asyncio
async def test_wrapper_retains_triage_artifact_when_applicability_fails(tmp_path: Path):
    """A validated first stage is written before the second stage runs."""

    with pytest.raises(RuntimeError):
        await run_two_stage_smoke(_result(), analysis_run_id=uuid.uuid4(), triage_agent=FakeTriage(), applicability_agent=FakeApplicability(fail=True), applicability_tools=object(), output_dir=tmp_path)
    assert (tmp_path / "term-triage-result.json").exists()
    assert not (tmp_path / "term-applicability-result.json").exists()


def test_diagnostics_directory_is_project_rooted():
    """Diagnostic paths do not depend on whether the process starts in src/."""

    path = diagnostics_directory()
    assert path.is_absolute() and path.as_posix().endswith("src/tests/test_results/term_applicability/of-0006/" + path.name)


@pytest.mark.asyncio
async def test_mismatched_triage_stops_before_applicability(tmp_path: Path):
    """A trusted identity mismatch prevents construction/invocation of stage two."""

    applicability = FakeApplicability()
    with pytest.raises(ValueError, match="triage result failed"):
        await run_two_stage_smoke(
            _result(),
            analysis_run_id=uuid.uuid4(),
            triage_agent=MismatchedTriage(),
            applicability_agent=applicability,
            applicability_tools=object(),
            output_dir=tmp_path,
        )
    assert applicability.task is None
    assert not (tmp_path / "term-applicability-result.json").exists()


def test_diagnostics_directory_ignores_current_working_directory(monkeypatch, tmp_path: Path):
    """The same repository-rooted path is selected from any process cwd."""

    expected_suffix = "src/tests/test_results/term_applicability/of-0006"
    monkeypatch.chdir(tmp_path)
    assert expected_suffix in diagnostics_directory().as_posix()


@pytest.mark.asyncio
async def test_applicability_failure_writes_bounded_runtime_summary(tmp_path: Path):
    """A typed failure preserves timing/tool/finalizer facts but no traceback or result."""

    runtime = TermApplicabilityRuntimeSummary(
        request_id=uuid.uuid4(), started_at=datetime.now(UTC),
        successful_tool_call_order=("get_term_evidence_region", "get_candidate_evidence_region"),
        finalization_submission_count=1, correction_attempt_count=1,
        terminal_reason="timeout", terminal_kind="timeout", total_elapsed_ms=150000,
        failure_stage="finalizer_not_called", structured_output_present=False,
    )
    output = tmp_path / "run"
    with pytest.raises(TermApplicabilityRuntimeError):
        await run_two_stage_smoke(
            _result(), analysis_run_id=uuid.uuid4(), triage_agent=FakeTriage(),
            applicability_agent=FailingApplicability(runtime), applicability_tools=object(), output_dir=output,
        )
    summary_path = output / "term-applicability-summary.json"
    payload = json.loads(summary_path.read_text())
    assert payload["status"] == "failed"
    assert payload["error_code"] == "term_applicability_timeout"
    assert payload["successful_tool_call_order"] == ["get_term_evidence_region", "get_candidate_evidence_region"]
    assert payload["total_elapsed_ms"] == 150000
    assert payload["terminal_reason"] == "timeout"
    assert not (output / "term-applicability-result.json").exists()
    assert not (output / "term-applicability-report.md").exists()
    serialized = summary_path.read_text()
    assert "traceback" not in serialized.lower()
    assert "generated failure" not in serialized
    assert (output / "term-triage-result.json").exists()
    assert (output / "term-triage-summary.json").exists()
