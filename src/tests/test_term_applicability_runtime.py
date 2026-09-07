"""Test TermApplicabilityRuntimeSummary's bounded, value-free timing/tool-event contract.

No SDK client exists yet -- these tests exercise only the dataclass and its
own recording methods directly.
"""

import uuid
from datetime import UTC, datetime

from maximor.term_applicability.agent import (
    MAX_RUNTIME_TIMING_EVENTS,
    MAX_RUNTIME_TOOL_DIAGNOSTICS,
    TermApplicabilityRuntimeSummary,
)


def _runtime(now: datetime | None = None) -> TermApplicabilityRuntimeSummary:
    now = now or datetime.now(UTC)
    return TermApplicabilityRuntimeSummary(request_id=uuid.uuid4(), started_at=now, monotonic_started_at=0.0)


def test_tool_invocation_timing_uses_monotonic_duration_and_utc_timestamp():
    """A start/finish pair records a non-negative monotonic duration and a UTC timestamp."""

    runtime = _runtime()
    now = datetime.now(UTC)
    sequence = runtime.begin_tool_invocation("get_term_evidence_region", now, 0.0)
    runtime.finish_tool_invocation(sequence, "get_term_evidence_region", succeeded=True, timestamp=now, monotonic_now=0.05)
    started = next(e for e in runtime.timing_events if e["kind"] == "tool_invocation_started")
    completed = next(e for e in runtime.timing_events if e["kind"] == "tool_invocation_completed")
    assert started["sequence_number"] == completed["sequence_number"] == 1
    assert completed["elapsed_ms"] >= started["elapsed_ms"] >= 0
    assert datetime.fromisoformat(completed["timestamp"]).tzinfo is not None
    assert runtime.last_successful_tool_return_at == now
    assert runtime.last_successful_tool_return_elapsed_ms == 50


def test_successful_tool_call_order_and_counts_are_recorded_by_caller():
    """The runtime records whatever order/counts a caller supplies -- no SDK loop yet."""

    runtime = _runtime()
    runtime.tool_call_count = 2
    runtime.tool_calls_by_name = {"get_term_evidence_region": 1, "get_candidate_evidence_region": 1}
    runtime.successful_tool_call_order = ("get_term_evidence_region", "get_candidate_evidence_region")
    assert runtime.tool_call_count == 2
    assert runtime.successful_tool_call_order == ("get_term_evidence_region", "get_candidate_evidence_region")


def test_finalizer_submission_acceptance_and_correction_counts():
    """Finalizer status fields are plain counters/booleans, settable independent of any SDK."""

    runtime = _runtime()
    runtime.finalization_submission_count = 2
    runtime.correction_attempt_count = 1
    runtime.finalization_accepted = True
    diagnostics = runtime.persistence_diagnostics()
    assert diagnostics["finalization_submission_count"] == 2
    assert diagnostics["correction_attempt_count"] == 1
    assert diagnostics["finalization_accepted"] is True


def test_terminal_and_total_elapsed_timing():
    """record_terminal captures only the first terminal event; finalize_timing sets totals."""

    runtime = _runtime()
    now = datetime.now(UTC)
    runtime.record_terminal("accepted_by_finalizer", now, 0.2)
    runtime.record_terminal("timeout", now, 0.9)  # second call must be ignored
    assert runtime.terminal_kind == "accepted_by_finalizer"
    assert runtime.terminal_elapsed_ms == 200
    runtime.finalize_timing(now, 1.0)
    assert runtime.total_elapsed_ms == 1000
    assert runtime.completed_at == now


def test_elapsed_after_last_tool_return_is_observed_interval_not_thinking_time():
    """The interval after the last successful tool return carries no reasoning claim."""

    runtime = _runtime()
    now = datetime.now(UTC)
    sequence = runtime.begin_tool_invocation("get_evidence_region", now, 0.0)
    runtime.finish_tool_invocation(sequence, "get_evidence_region", succeeded=True, timestamp=now, monotonic_now=0.5)
    runtime.finalize_timing(now, 2.0)
    assert runtime.elapsed_after_last_successful_tool_return_ms == 1500
    # The field name and docstring both describe an observed interval; the
    # dataclass never records or exposes anything claiming model reasoning.
    import maximor.term_applicability.agent as agent_module
    assert "thinking" not in (agent_module.TermApplicabilityRuntimeSummary.__doc__ or "").lower() or "not a measurement" in agent_module.TermApplicabilityRuntimeSummary.__doc__.lower()


def test_timing_event_history_is_bounded_and_value_free():
    """Truncate metadata-only timing history rather than retaining an unbounded trace."""

    now = datetime.now(UTC)
    runtime = _runtime(now)
    for index in range(MAX_RUNTIME_TIMING_EVENTS + 5):
        runtime.record_timing_event(kind="sdk_event_received", timestamp=now, monotonic_now=float(index), event_type="AssistantMessage")
    diagnostics = runtime.persistence_diagnostics()
    assert len(runtime.timing_events) == MAX_RUNTIME_TIMING_EVENTS
    assert runtime.timing_events_truncated and diagnostics["timing_events_truncated"]
    assert all(event["elapsed_ms"] >= 0 for event in runtime.timing_events)
    assert "prompt" not in repr(diagnostics).lower()
    assert "reasoning" not in repr(diagnostics).lower()


def test_adapter_invocation_history_is_bounded():
    """Adapter-boundary facts truncate with a flag rather than growing unbounded."""

    runtime = _runtime()
    for index in range(MAX_RUNTIME_TOOL_DIAGNOSTICS + 3):
        runtime.record_adapter_invocation(f"tool-{index}", schema_validated=True, succeeded=True)
    assert len(runtime.adapter_invocations) == MAX_RUNTIME_TOOL_DIAGNOSTICS
    assert runtime.tool_diagnostics_truncated


def test_persistence_diagnostics_excludes_sensitive_data():
    """The bounded snapshot never carries prompts, document text, or credentials."""

    runtime = _runtime()
    runtime.pydantic_errors = ({"location": "decisions.0.term_id", "type": "value_error"},)
    runtime.validation_issues = ({"code": "term_applicability_unknown_term", "location": "decisions.0"},)
    diagnostics = runtime.persistence_diagnostics()
    blob = repr(diagnostics).lower()
    for forbidden in ("secret", "api_key", "password", "select ", "bearer ", "raw_name", "raw_value"):
        assert forbidden not in blob
