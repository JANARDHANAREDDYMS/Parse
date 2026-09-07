"""Orchestrate the non-persistent two-stage term smoke test.

This wrapper receives an already loaded `DocumentAnalysisResult`, builds a
separate triage task and full applicability task from that same result, and
writes staged diagnostics. It never loads files, writes to the database, or
invokes an agent unless the caller explicitly injects one.
"""

from __future__ import annotations

import json
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from maximor.config import PROJECT_ROOT
from maximor.document_analysis.schemas import DocumentAnalysisResult
from maximor.term_applicability.contracts import (
    build_selected_term_applicability_task,
    build_term_applicability_task,
)
from maximor.term_applicability.errors import TermApplicabilityRuntimeError, TermApplicabilityValidationError
from maximor.term_applicability.schemas import TermApplicabilityResult
from maximor.term_applicability.validation import validate_term_applicability_result
from maximor.term_applicability.versions import TERM_APPLICABILITY_TASK_SCHEMA_VERSION
from maximor.term_triage.contracts import build_term_triage_task
from maximor.term_triage.schemas import TermTriageResult
from maximor.term_triage.validation import validate_term_triage_result
from maximor.term_triage.versions import TERM_TRIAGE_TASK_SCHEMA_VERSION


class _TriageRunner(Protocol):
    """Provide the triage operation used by the staged wrapper."""

    async def triage(self, task: Any) -> TermTriageResult: ...


class _ApplicabilityRunner(Protocol):
    """Provide the applicability operation used by the staged wrapper."""

    async def resolve(self, task: Any, tools: Any) -> TermApplicabilityResult: ...


def diagnostics_directory(timestamp: datetime | None = None) -> Path:
    """Return a repository-rooted timestamped diagnostics directory."""

    stamp = (timestamp or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return PROJECT_ROOT / "src/tests/test_results/term_applicability/of-0006" / stamp


def _runtime_summary(execution: Any) -> dict[str, Any]:
    """Extract bounded runtime metadata from an execution when available."""

    runtime = getattr(execution, "runtime", None)
    return runtime.persistence_diagnostics() if runtime is not None else {}


def _write(path: Path, payload: Any) -> None:
    """Write deterministic UTF-8 JSON diagnostics."""

    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _failure_summary(error: BaseException) -> dict[str, Any]:
    """Build a value-free applicability failure snapshot from attached runtime facts."""

    runtime = getattr(error, "runtime", None)
    details = runtime.persistence_diagnostics() if runtime is not None and hasattr(runtime, "persistence_diagnostics") else {}
    # Keep this allowlist aligned with the runtime's approved diagnostics; in
    # particular, never serialize the exception message or traceback.
    fields = (
        "failure_stage", "terminal_reason", "sdk_terminal_reason", "terminal_kind", "total_elapsed_ms",
        "session_initialization_elapsed_ms", "session_initialization_succeeded",
        "successful_tool_call_order", "retrieved_term_evidence_ids",
        "retrieved_candidate_evidence_ids", "sdk_event_types", "timing_events",
        "timing_events_truncated", "finalization_submission_count",
        "finalization_accepted", "correction_attempt_count",
        "structured_output_present", "output_field_names", "output_collection_counts",
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
        "output_tokens", "cost_usd", "tool_diagnostics_truncated",
    )
    snapshot = {name: details.get(name) for name in fields}
    if snapshot["terminal_reason"] is None:
        snapshot["terminal_reason"] = snapshot["sdk_terminal_reason"]
    snapshot.update({
        "status": "failed",
        "error_type": type(error).__name__,
        "error_code": getattr(error, "code", None),
    })
    return snapshot


def _write_applicability_failure(output: Path, error: BaseException) -> None:
    """Persist bounded applicability failure diagnostics before re-raising."""

    output.mkdir(parents=True, exist_ok=True)
    _write(output / "term-applicability-summary.json", _failure_summary(error))


async def run_two_stage_smoke(
    analysis_result: DocumentAnalysisResult,
    *,
    analysis_run_id: UUID,
    triage_agent: _TriageRunner,
    applicability_agent: _ApplicabilityRunner,
    applicability_tools: Any,
    output_dir: Path | None = None,
) -> Path:
    """Run triage then applicability, writing diagnostics only after each success."""

    output = (output_dir if output_dir is not None else diagnostics_directory())
    # Keep caller-provided relative paths deterministic as well; diagnostics
    # must never depend on whether the process starts in the repository root
    # or in ``src``.
    if not output.is_absolute():
        output = (PROJECT_ROOT / output).resolve()
    triage_task = build_term_triage_task(
        result=analysis_result,
        analysis_run_id=analysis_run_id,
        schema_version=TERM_TRIAGE_TASK_SCHEMA_VERSION,
    )
    triage_result = await triage_agent.triage(triage_task)
    triage_issues = validate_term_triage_result(triage_task, triage_result)
    if triage_issues:
        raise ValueError("triage result failed deterministic validation")
    output.mkdir(parents=True, exist_ok=True)
    _write(output / "term-triage-result.json", triage_result.model_dump(mode="json"))
    _write(output / "term-triage-summary.json", {
        "status": "succeeded",
        "analysis_run_id": str(analysis_run_id),
        "decision_count": len(triage_result.decisions),
    })

    full_applicability_task = build_term_applicability_task(
        result=analysis_result,
        analysis_run_id=analysis_run_id,
        schema_version=TERM_APPLICABILITY_TASK_SCHEMA_VERSION,
    )
    if (
        full_applicability_task.organization_id,
        full_applicability_task.document_id,
        full_applicability_task.preprocessing_run_id,
        full_applicability_task.analysis_run_id,
    ) != (
        triage_result.organization_id,
        triage_result.document_id,
        triage_result.preprocessing_run_id,
        triage_result.analysis_run_id,
    ):
        raise ValueError("triage and applicability task identities do not match")
    selected = build_selected_term_applicability_task(full_applicability_task, triage_result)
    try:
        applicability_result = await applicability_agent.resolve(selected, applicability_tools)
    except (TermApplicabilityRuntimeError, TermApplicabilityValidationError, asyncio.TimeoutError, asyncio.CancelledError) as error:
        _write_applicability_failure(output, error)
        raise
    except Exception as error:
        # Preserve diagnostics for injected/local failures without exposing
        # arbitrary exception text; the caller still receives the original
        # exception for test control flow.
        _write_applicability_failure(output, error)
        raise
    applicability_issues = validate_term_applicability_result(selected, applicability_result)
    if applicability_issues:
        raise ValueError("applicability result failed deterministic validation")
    output.mkdir(parents=True, exist_ok=True)
    _write(output / "term-applicability-result.json", applicability_result.model_dump(mode="json"))
    _write(output / "term-applicability-summary.json", {
        "status": "succeeded",
        "analysis_run_id": str(analysis_run_id),
        "decision_count": len(applicability_result.decisions),
        "candidate_fact_bundle_count": len(applicability_result.candidate_commercial_facts),
    })
    _write(output / "term-applicability-report.md", "# Term applicability smoke test\n\nBoth stages passed deterministic validation.\n")
    return output


__all__ = ["diagnostics_directory", "run_two_stage_smoke"]
