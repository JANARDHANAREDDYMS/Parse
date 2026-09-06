"""Test safe failure serialization for the isolated term-applicability wrapper."""

import json
import uuid
from datetime import UTC, datetime

from maximor.document_analysis.agent import DocumentAnalysisRuntimeSummary
from maximor.document_analysis.errors import DocumentAnalysisRuntimeError
from maximor.document_analysis.term_applicability_smoke import write_failure_summary


def _ids() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return generated scope identifiers for wrapper tests."""

    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_timeout_with_runtime_writes_bounded_diagnostics(tmp_path):
    """Attached runtime facts survive a timeout without sensitive values."""

    runtime = DocumentAnalysisRuntimeSummary(
        request_id=uuid.uuid4(), started_at=datetime.now(UTC),
        tool_call_count=2,
        tool_calls_by_name={"get_document_overview": 1, "search_document": 1},
        successful_tool_call_order=("get_document_overview", "search_document"),
        failure_stage="completion_gate_failure", total_elapsed_ms=120000,
    )
    error = DocumentAnalysisRuntimeError("document_analysis_timeout", runtime=runtime)
    org, doc, run = _ids()
    path = tmp_path / "summary.json"
    write_failure_summary(error, path, organization_id=org, document_id=doc, preprocessing_run_id=run)
    payload = json.loads(path.read_text())
    assert payload["error_code"] == "document_analysis_timeout"
    assert payload["successful_tool_call_order"] == ["get_document_overview", "search_document"]
    assert payload["total_elapsed_ms"] == 120000
    assert "runtime" not in json.dumps(payload)


def test_error_without_runtime_remains_safely_nullable(tmp_path):
    """An exception without attached runtime produces bounded null diagnostics."""

    error = DocumentAnalysisRuntimeError("document_analysis_timeout")
    org, doc, run = _ids()
    path = tmp_path / "summary.json"
    write_failure_summary(error, path, organization_id=org, document_id=doc, preprocessing_run_id=run)
    payload = json.loads(path.read_text())
    assert payload["error_code"] == "document_analysis_timeout"
    assert payload["total_elapsed_ms"] is None
    assert payload["successful_tool_call_order"] == []
