"""Write bounded diagnostics for the isolated term-applicability smoke test.

The wrapper receives a completed agent execution or a safe document-analysis
exception and writes only the runtime summary already approved by the agent
runtime, plus (on success only) the validated in-memory result the agent
itself produced. It does not invoke Claude, access documents, or persist
database rows.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from maximor.document_analysis.errors import (
    DocumentAnalysisRuntimeError,
    DocumentAnalysisValidationError,
)

if TYPE_CHECKING:
    from maximor.document_analysis.agent import DocumentAnalysisExecution


def _runtime_snapshot(error: Exception) -> dict[str, Any]:
    """Return a bounded runtime snapshot, or safe nulls when none was attached."""

    runtime = getattr(error, "runtime", None)
    if runtime is None:
        return {
            "failure_stage": None,
            "successful_tool_call_order": [],
            "successful_overview_call_count": None,
            "successful_content_retrieval_count": None,
            "finalization_submission_count": None,
            "finalization_accepted": None,
            "correction_attempt_count": None,
            "validation_issue_codes": [],
            "validation_issues": [],
            "sdk_terminal_reason": None,
            "terminal_kind": None,
            "total_elapsed_ms": None,
            "session_initialization_elapsed_ms": None,
            "input_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
            "timing_events": [],
            "sdk_event_types": [],
        }
    details = runtime.persistence_diagnostics()
    allowed = (
        "failure_stage", "successful_tool_call_order",
        "successful_overview_call_count", "successful_content_retrieval_count",
        "finalization_submission_count", "finalization_accepted",
        "correction_attempt_count", "validation_issue_codes", "validation_issues",
        "sdk_terminal_reason", "terminal_kind", "total_elapsed_ms",
        "session_initialization_elapsed_ms", "input_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens",
        "cost_usd", "timing_events", "sdk_event_types",
    )
    return {key: details.get(key) for key in allowed}


def write_failure_summary(
    error: DocumentAnalysisRuntimeError | DocumentAnalysisValidationError,
    output_path: Path,
    *,
    organization_id: UUID,
    document_id: UUID,
    preprocessing_run_id: UUID,
) -> None:
    """Serialize one safe smoke-test failure summary without exception details."""

    payload = {
        "organization_id": str(organization_id),
        "document_id": str(document_id),
        "preprocessing_run_id": str(preprocessing_run_id),
        "error_code": error.code,
        **_runtime_snapshot(error),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _evidence_id(evidence: Any) -> str | None:
    """Return one addressable evidence identifier, never bounding-box or source text."""

    return evidence.block_id or evidence.table_id


def _safe_terms_projection(result: Any) -> list[dict[str, Any]]:
    """Return per-term scope/candidate/evidence identifiers without raw term content."""

    return [
        {
            "term_id": term.term_id,
            "applicability_scope": term.applicability_scope.value,
            "applies_to_candidate_ids": list(term.applies_to_candidate_ids),
            "evidence_ids": [_evidence_id(item) for item in term.evidence],
        }
        for term in result.global_terms
    ]


def write_success_files(
    execution: "DocumentAnalysisExecution",
    *,
    result_path: Path,
    summary_path: Path,
    organization_id: UUID,
    document_id: UUID,
    preprocessing_run_id: UUID,
    timeout_seconds: float,
) -> None:
    """Serialize the validated result and a safe, content-free summary on success."""

    result, runtime = execution.result, execution.runtime
    diagnostics = runtime.persistence_diagnostics()
    terms = _safe_terms_projection(result)
    scope_counts = Counter(term["applicability_scope"] for term in terms)

    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    summary = {
        "organization_id": str(organization_id),
        "document_id": str(document_id),
        "preprocessing_run_id": str(preprocessing_run_id),
        "timeout_seconds": timeout_seconds,
        "status": "completed",
        "successful_tool_call_order": list(diagnostics["successful_tool_call_order"]),
        "finalization_submission_count": diagnostics["finalization_submission_count"],
        "finalization_accepted": diagnostics["finalization_accepted"],
        "sdk_terminal_reason": diagnostics["sdk_terminal_reason"],
        "terminal_kind": diagnostics["terminal_kind"],
        "total_elapsed_ms": diagnostics["total_elapsed_ms"],
        "session_initialization_elapsed_ms": diagnostics["session_initialization_elapsed_ms"],
        "correction_attempt_count": diagnostics["correction_attempt_count"],
        "output_collection_counts": diagnostics["output_collection_counts"],
        "tokens": {
            "input_tokens": runtime.input_tokens,
            "output_tokens": runtime.output_tokens,
            "cache_creation_input_tokens": runtime.cache_creation_input_tokens,
            "cache_read_input_tokens": runtime.cache_read_input_tokens,
        },
        "cost_usd": runtime.cost_usd,
        "model_usage": runtime.model_usage or {},
        "term_counts_by_scope": {
            "document": scope_counts.get("document", 0),
            "candidate": scope_counts.get("candidate", 0),
            "unknown": scope_counts.get("unknown", 0),
        },
        "terms": terms,
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = ["write_failure_summary", "write_success_files"]
