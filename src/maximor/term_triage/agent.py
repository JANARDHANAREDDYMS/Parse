"""Run a bounded Claude Agent SDK loop over one fixed term-triage task.

The concrete agent receives one `TermTriageTask` (a whole completed analysis
run's candidates and terms, batched) and returns a validated
`TermTriageResult` plus small operational metrics. It never persists
decisions, never connects to workers, and never exposes arbitrary files,
SQL, or unrestricted tools.

Unlike `document_analysis`/`sku_mapping`/`term_applicability`'s agents, this
one has **no read-only tools at all** -- no evidence retrieval, no search, no
lookup of any kind. It is a compact reasoning stage over the compact task
context already in the prompt, with exactly one tool: the guarded
`finalize_term_triage` finalizer. This is a deliberate scope boundary: triage
must never reach for evidence to justify a classification -- that grounding
work belongs entirely to `TermApplicabilityAgent`, on the narrower selected
subset triage produces.
"""

import asyncio
import copy
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

import claude_agent_sdk as sdk
from pydantic import ValidationError

from maximor.config import DatabaseSettings
from maximor.term_triage.contracts import TermTriageTask
from maximor.term_triage.errors import (
    TermTriageConfigurationError,
    TermTriageNotConfiguredError,
    TermTriageRuntimeError,
    TermTriageValidationError,
)
from maximor.term_triage.schemas import TermTriageResult
from maximor.term_triage.validation import validate_term_triage_result
from maximor.term_triage.versions import TERM_TRIAGE_RESULT_SCHEMA_VERSION

MAX_RUNTIME_TIMING_EVENTS = 200
MAX_RUNTIME_TOOL_DIAGNOSTICS = 100
FINALIZER_RESULT_GRACE_SECONDS = 2.0

FINALIZER_TOOL_NAME = "finalize_term_triage"
TOOL_NAMES = (FINALIZER_TOOL_NAME,)
MCP_SERVER_NAME = "maximor_term_triage"
MCP_TOOL_NAMES = tuple(f"mcp__{MCP_SERVER_NAME}__{name}" for name in TOOL_NAMES)
OUTPUT_COLLECTION_NAMES = ("decisions",)
TRUSTED_IDENTITY_FIELDS = {"schema_version", "organization_id", "document_id", "preprocessing_run_id", "analysis_run_id"}


class TermTriageAgent(Protocol):
    """Classify every term in one fixed task as metadata, potential line item, or uncertain."""

    async def triage(self, task: TermTriageTask) -> TermTriageResult:
        """Return a validated triage result without persisting anything."""

        ...


class UnconfiguredTermTriageAgent:
    """Fail explicitly until a concrete tool-using agent implementation is configured."""

    async def triage(self, task: TermTriageTask) -> TermTriageResult:
        """Raise a typed failure and never synthesize a fake successful result."""

        del task
        raise TermTriageNotConfiguredError


@dataclass
class _FinalizationCapture:
    """Keep one request-scoped accepted result in memory without persistence."""

    accepted_event: asyncio.Event = field(default_factory=asyncio.Event)
    rejection_event: asyncio.Event = field(default_factory=asyncio.Event)
    accepted_result: TermTriageResult | None = None
    correctable_rejection_seen: bool = False


@dataclass
class TermTriageRuntimeSummary:
    """Keep bounded operational facts for one invocation, never document bodies or secrets.

    Structurally mirrors `DocumentAnalysisRuntimeSummary`/`SkuMappingRuntimeSummary`/
    `TermApplicabilityRuntimeSummary` wherever the concept is shared, and
    omits everything tied to evidence retrieval (there are no read-only
    tools here to retrieve anything with). Durations use a monotonic clock;
    correlation timestamps are UTC. `elapsed_after_last_successful_tool_return_ms`
    is an observed interval, never a claim about model reasoning time.
    """

    request_id: uuid.UUID
    started_at: datetime
    monotonic_started_at: float = field(default_factory=time.monotonic, repr=False)
    completed_at: datetime | None = None
    turn_count: int = 0
    tool_call_count: int = 0
    tool_calls_by_name: dict[str, int] = field(default_factory=dict)
    successful_tool_call_order: tuple[str, ...] = ()
    failed_tool_call_count: int = 0
    failed_tool_calls_by_name: dict[str, int] = field(default_factory=dict)
    input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    output_tokens: int | None = None
    model_usage: dict[str, dict[str, int | float | str]] = field(default_factory=dict)
    cost_usd: float | None = None
    terminal_reason: str | None = None
    sdk_terminal_reason: str | None = None
    mcp_server_status: str = "not_started"
    announced_tool_names: tuple[str, ...] = ()
    setting_sources: tuple[str, ...] = ()
    tool_event_types: tuple[str, ...] = ()
    correction_attempt_count: int = 0
    failure_stage: str | None = None
    pydantic_errors: tuple[dict[str, str], ...] = ()
    validation_issue_codes: tuple[str, ...] = ()
    validation_issues: tuple[dict[str, str], ...] = ()
    structured_output_present: bool = False
    output_field_names: tuple[str, ...] = ()
    output_collection_counts: dict[str, int] = field(default_factory=dict)
    finalization_submission_count: int = 0
    finalization_accepted: bool = False
    session_initialization_started_at: datetime | None = None
    session_initialization_completed_at: datetime | None = None
    session_initialization_elapsed_ms: int | None = None
    session_initialization_succeeded: bool | None = None
    last_successful_tool_return_at: datetime | None = None
    last_successful_tool_return_elapsed_ms: int | None = None
    terminal_at: datetime | None = None
    terminal_elapsed_ms: int | None = None
    terminal_kind: str | None = None
    total_elapsed_ms: int | None = None
    elapsed_after_last_successful_tool_return_ms: int | None = None
    timing_events: list[dict[str, Any]] = field(default_factory=list)
    timing_events_truncated: bool = False
    sdk_tool_use_events: list[dict[str, Any]] = field(default_factory=list)
    adapter_invocations: list[dict[str, Any]] = field(default_factory=list)
    tool_diagnostics_truncated: bool = False
    sdk_system_categories: tuple[str, ...] = ()
    _next_tool_sequence: int = field(default=0, repr=False)
    _next_sdk_tool_sequence: int = field(default=0, repr=False)
    _session_initialization_monotonic_started_at: float | None = field(default=None, repr=False)

    def elapsed_ms(self, monotonic_now: float) -> int:
        """Return a non-negative duration from the monotonic execution start."""

        return max(0, int((monotonic_now - self.monotonic_started_at) * 1000))

    def record_timing_event(
        self,
        *,
        kind: str,
        timestamp: datetime,
        monotonic_now: float,
        tool_name: str | None = None,
        sequence_number: int | None = None,
        succeeded: bool | None = None,
        event_type: str | None = None,
    ) -> None:
        """Append one bounded, metadata-only timing event for correlation."""

        if len(self.timing_events) >= MAX_RUNTIME_TIMING_EVENTS:
            self.timing_events_truncated = True
            return
        event: dict[str, Any] = {
            "kind": kind,
            "timestamp": timestamp.isoformat(),
            "elapsed_ms": self.elapsed_ms(monotonic_now),
        }
        if tool_name is not None:
            event["tool_name"] = tool_name
        if sequence_number is not None:
            event["sequence_number"] = sequence_number
        if succeeded is not None:
            event["succeeded"] = succeeded
        if event_type is not None:
            event["event_type"] = event_type
        self.timing_events.append(event)

    def begin_tool_invocation(self, name: str, timestamp: datetime, monotonic_now: float) -> int:
        """Record the start of one local adapter call and return its sequence number."""

        self._next_tool_sequence += 1
        sequence_number = self._next_tool_sequence
        self.record_timing_event(
            kind="tool_invocation_started", timestamp=timestamp,
            monotonic_now=monotonic_now, tool_name=name,
            sequence_number=sequence_number,
        )
        return sequence_number

    def finish_tool_invocation(
        self, sequence_number: int, name: str, *, succeeded: bool,
        timestamp: datetime, monotonic_now: float,
    ) -> None:
        """Record local adapter completion without claiming model receipt of its result."""

        self.record_timing_event(
            kind="tool_invocation_completed", timestamp=timestamp,
            monotonic_now=monotonic_now, tool_name=name,
            sequence_number=sequence_number, succeeded=succeeded,
        )
        if succeeded:
            self.last_successful_tool_return_at = timestamp
            self.last_successful_tool_return_elapsed_ms = self.elapsed_ms(monotonic_now)

    def record_adapter_invocation(
        self, name: str, *, schema_validated: bool, succeeded: bool,
        failure_category: str | None = None,
    ) -> None:
        """Record bounded adapter-boundary facts without arguments or result bodies."""

        if len(self.adapter_invocations) >= MAX_RUNTIME_TOOL_DIAGNOSTICS:
            self.tool_diagnostics_truncated = True
            return
        self.adapter_invocations.append({
            "tool_name": name[:100],
            "schema_validation_reached": schema_validated,
            "adapter_succeeded": succeeded,
            "failure_category": failure_category[:100] if failure_category else None,
        })

    def record_terminal(
        self, kind: str, timestamp: datetime, monotonic_now: float,
    ) -> None:
        """Capture the first terminal result, timeout, cancellation, or failure event."""

        if self.terminal_at is not None:
            return
        self.terminal_at = timestamp
        self.terminal_elapsed_ms = self.elapsed_ms(monotonic_now)
        self.terminal_kind = kind
        self.record_timing_event(
            kind="terminal", timestamp=timestamp, monotonic_now=monotonic_now,
            event_type=kind,
        )

    def finalize_timing(self, timestamp: datetime, monotonic_now: float) -> None:
        """Record total elapsed time and post-tool elapsed time without attributing cause."""

        self.completed_at = timestamp
        self.total_elapsed_ms = self.elapsed_ms(monotonic_now)
        if self.last_successful_tool_return_elapsed_ms is not None:
            self.elapsed_after_last_successful_tool_return_ms = max(
                0, self.total_elapsed_ms - self.last_successful_tool_return_elapsed_ms,
            )

    def persistence_diagnostics(self) -> dict[str, Any]:
        """Return a bounded value-free operational snapshot safe for persistence."""

        return {
            "failure_stage": self.failure_stage,
            "pydantic_errors": list(self.pydantic_errors[:50]),
            "validation_issue_codes": list(self.validation_issue_codes[:50]),
            "validation_issues": list(self.validation_issues[:50]),
            "successful_tool_call_order": list(self.successful_tool_call_order[:100]),
            "structured_output_present": self.structured_output_present,
            "output_field_names": list(self.output_field_names[:50]),
            "output_collection_counts": dict(self.output_collection_counts),
            "sdk_terminal_reason": self.sdk_terminal_reason or self.terminal_reason,
            "sdk_event_types": list(self.tool_event_types[:50]),
            "mcp_server_status": self.mcp_server_status,
            "correction_attempt_count": self.correction_attempt_count,
            "finalization_submission_count": self.finalization_submission_count,
            "finalization_accepted": self.finalization_accepted,
            "session_initialization_started_at": self._timestamp(self.session_initialization_started_at),
            "session_initialization_completed_at": self._timestamp(self.session_initialization_completed_at),
            "session_initialization_elapsed_ms": self.session_initialization_elapsed_ms,
            "session_initialization_succeeded": self.session_initialization_succeeded,
            "last_successful_tool_return_at": self._timestamp(self.last_successful_tool_return_at),
            "last_successful_tool_return_elapsed_ms": self.last_successful_tool_return_elapsed_ms,
            "terminal_at": self._timestamp(self.terminal_at),
            "terminal_elapsed_ms": self.terminal_elapsed_ms,
            "terminal_kind": self.terminal_kind,
            "total_elapsed_ms": self.total_elapsed_ms,
            "elapsed_after_last_successful_tool_return_ms": self.elapsed_after_last_successful_tool_return_ms,
            "timing_events": list(self.timing_events),
            "timing_events_truncated": self.timing_events_truncated,
            "sdk_tool_use_events": list(self.sdk_tool_use_events),
            "adapter_invocations": list(self.adapter_invocations),
            "sdk_system_categories": list(self.sdk_system_categories),
            "tool_diagnostics_truncated": self.tool_diagnostics_truncated,
        }

    @staticmethod
    def _timestamp(value: datetime | None) -> str | None:
        """Serialize one UTC correlation timestamp without accepting arbitrary values."""

        return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class TermTriageExecution:
    """Return a validated result and non-semantic runtime summary together."""

    result: TermTriageResult
    runtime: TermTriageRuntimeSummary


class ClaudeTermTriageAgent:
    """Use Claude through exactly one in-memory submission tool -- no evidence tools at all.

    Claude receives the fixed task's compact candidate/term summaries
    directly in the prompt; trusted identity and version fields are captured
    from the task, never from Claude. This class performs no persistence,
    scheduling, or worker integration.
    """

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        client_factory: Callable[[sdk.ClaudeAgentOptions], Any] = sdk.ClaudeSDKClient,
        monotonic_clock: Callable[[], float] = time.monotonic,
        utc_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Receive centralized settings and an injectable stateful SDK client factory."""
        self._settings = settings
        self._client_factory = client_factory
        self._monotonic_clock = monotonic_clock
        self._utc_clock = utc_clock

    def build_options(self, task: TermTriageTask) -> sdk.ClaudeAgentOptions:
        """Build isolated SDK options with exactly one guarded finalizer and no readers."""
        key = self._settings.anthropic_api_key
        if key is None or not key.get_secret_value():
            raise TermTriageConfigurationError
        adapters, runtime = self._build_adapters(task)
        server = sdk.create_sdk_mcp_server(MCP_SERVER_NAME, tools=adapters)
        options = sdk.ClaudeAgentOptions(
            tools=[], allowed_tools=list(MCP_TOOL_NAMES), disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch"],
            mcp_servers={MCP_SERVER_NAME: server}, strict_mcp_config=True,
            model=self._settings.term_triage_model, max_turns=self._settings.term_triage_max_turns,
            max_thinking_tokens=self._settings.term_triage_max_thinking_tokens,
            max_budget_usd=self._settings.term_triage_max_budget_usd,
            cwd=self._settings.term_triage_project_root.resolve(), settings=None,
            setting_sources=["project"], skills=["term-triage"], plugins=[],
            env={"ANTHROPIC_API_KEY": key.get_secret_value()},
        )
        runtime.mcp_server_status = "configured"
        runtime.setting_sources = tuple(options.setting_sources or ())
        object.__setattr__(options, "_maximor_runtime", runtime)
        object.__setattr__(options, "_maximor_adapters", adapters)
        return options

    async def triage(self, task: TermTriageTask) -> TermTriageResult:
        """Run triage and return the validated result required by the agent protocol."""
        return (await self.execute(task)).result

    async def execute(self, task: TermTriageTask) -> TermTriageExecution:
        """Run the bounded SDK loop and return a result with bounded runtime metrics."""
        options = self.build_options(task)
        runtime = getattr(options, "_maximor_runtime")
        result: TermTriageResult | None = None
        client: Any | None = None
        stage = "initialization"
        try:
            async with asyncio.timeout(self._settings.term_triage_timeout_seconds):
                initialization_started_at = self._utc_clock()
                runtime.session_initialization_started_at = initialization_started_at
                runtime._session_initialization_monotonic_started_at = self._monotonic_clock()
                runtime.record_timing_event(
                    kind="session_initialization_started",
                    timestamp=initialization_started_at,
                    monotonic_now=runtime._session_initialization_monotonic_started_at,
                )
                client = self._client_factory(options)
                local_tools = await self._configured_mcp_tool_names(options)
                if local_tools != tuple(sorted(TOOL_NAMES)):
                    raise TermTriageRuntimeError("term_triage_mcp_tool_discovery_failed", runtime=runtime)
                runtime.announced_tool_names = local_tools
                runtime.mcp_server_status = "local_verified"
                await client.connect()
                await self._confirm_mcp_ready(client, runtime)
                initialization_completed_at = self._utc_clock()
                runtime.session_initialization_completed_at = initialization_completed_at
                initialization_finished_monotonic = self._monotonic_clock()
                initialization_started_monotonic = runtime._session_initialization_monotonic_started_at
                runtime.session_initialization_elapsed_ms = max(
                    0,
                    int((
                        initialization_finished_monotonic
                        - (
                            initialization_started_monotonic
                            if initialization_started_monotonic is not None
                            else initialization_finished_monotonic
                        )
                    ) * 1000),
                )
                runtime.session_initialization_succeeded = True
                runtime.record_timing_event(
                    kind="session_initialization_completed",
                    timestamp=initialization_completed_at,
                    monotonic_now=initialization_finished_monotonic, succeeded=True,
                )
                stage = "transport"
                await client.query(self._prompt(task))
                capture = getattr(runtime, "_finalization_capture")
                result = await self._await_finalization(client, runtime, capture)
        except TimeoutError:
            runtime.terminal_reason = "timeout"
            runtime.record_terminal("timeout", self._utc_clock(), self._monotonic_clock())
            raise TermTriageRuntimeError("term_triage_timeout", runtime=runtime) from None
        except TermTriageRuntimeError:
            runtime.record_terminal("failure", self._utc_clock(), self._monotonic_clock())
            raise
        except TermTriageValidationError:
            runtime.record_terminal("validation_failure", self._utc_clock(), self._monotonic_clock())
            raise
        except asyncio.CancelledError:
            runtime.terminal_reason = "cancelled"
            runtime.record_terminal("cancelled", self._utc_clock(), self._monotonic_clock())
            raise
        except Exception:
            code = "term_triage_sdk_initialization_failed" if stage == "initialization" else "term_triage_sdk_transport_failed"
            runtime.record_terminal("failure", self._utc_clock(), self._monotonic_clock())
            raise TermTriageRuntimeError(code, runtime=runtime) from None
        finally:
            if runtime.session_initialization_started_at is not None and runtime.session_initialization_completed_at is None:
                runtime.session_initialization_completed_at = self._utc_clock()
                initialization_finished_monotonic = self._monotonic_clock()
                initialization_started_monotonic = runtime._session_initialization_monotonic_started_at
                runtime.session_initialization_elapsed_ms = max(
                    0,
                    int((
                        initialization_finished_monotonic
                        - (
                            initialization_started_monotonic
                            if initialization_started_monotonic is not None
                            else initialization_finished_monotonic
                        )
                    ) * 1000),
                )
                runtime.session_initialization_succeeded = False
                runtime.record_timing_event(
                    kind="session_initialization_completed",
                    timestamp=runtime.session_initialization_completed_at,
                    monotonic_now=initialization_finished_monotonic, succeeded=False,
                )
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    # A cleanup failure cannot replace the safe primary outcome.
                    if runtime.terminal_reason is None:
                        runtime.terminal_reason = "cleanup_failed"
            runtime.finalize_timing(self._utc_clock(), self._monotonic_clock())
        if result is None:
            raise TermTriageValidationError(runtime=runtime)
        return TermTriageExecution(result=result, runtime=runtime)

    async def _await_finalization(
        self, client: Any, runtime: TermTriageRuntimeSummary,
        capture: _FinalizationCapture,
    ) -> TermTriageResult:
        """Wait for an accepted in-memory submission or a safe terminal SDK outcome."""

        response_task = asyncio.create_task(
            self._receive_terminal_result(client, runtime),
        )
        accepted_task = asyncio.create_task(capture.accepted_event.wait())
        rejected_task = asyncio.create_task(capture.rejection_event.wait())
        try:
            if capture.accepted_event.is_set():
                return await self._complete_accepted_finalization(
                    client, runtime, capture, response_task,
                )
            done, _ = await asyncio.wait(
                {response_task, accepted_task, rejected_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if accepted_task in done:
                return await self._complete_accepted_finalization(
                    client, runtime, capture, response_task,
                )
            if rejected_task in done:
                await self._cancel_response_task(client, response_task)
                raise TermTriageValidationError(runtime=runtime)
            terminal = await response_task
            if terminal is None:
                runtime.failure_stage = "finalizer_not_called"
                raise TermTriageRuntimeError(
                    "term_triage_finalizer_not_called", runtime=runtime,
                )
            if terminal.is_error or runtime.sdk_terminal_reason in {
                "max_turns", "timeout", "budget", "aborted_streaming", "aborted_tools",
            }:
                raise TermTriageRuntimeError(
                    self._terminal_code(runtime.sdk_terminal_reason, terminal), runtime=runtime,
                )
            runtime.failure_stage = "finalizer_not_called"
            raise TermTriageRuntimeError(
                "term_triage_finalizer_not_called", runtime=runtime,
            )
        finally:
            for task in (accepted_task, rejected_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(accepted_task, rejected_task, return_exceptions=True)

    async def _complete_accepted_finalization(
        self, client: Any, runtime: TermTriageRuntimeSummary,
        capture: _FinalizationCapture, response_task: asyncio.Task,
    ) -> TermTriageResult:
        """Return an accepted result after a short optional terminal-message grace period."""

        result = capture.accepted_result
        if result is None:
            raise TermTriageValidationError(runtime=runtime)
        runtime.terminal_reason = "accepted_by_finalizer"
        runtime.record_terminal(
            "accepted_by_finalizer", self._utc_clock(), self._monotonic_clock(),
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(response_task), timeout=FINALIZER_RESULT_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            await self._cancel_response_task(client, response_task)
        except Exception:
            # A post-accept terminal SDK failure cannot invalidate the captured result.
            pass
        return result

    async def _cancel_response_task(self, client: Any, response_task: asyncio.Task) -> None:
        """Interrupt the SDK then cancel its pending receive task without exposing details."""

        try:
            await client.interrupt()
        except Exception:
            pass
        if not response_task.done():
            response_task.cancel()
        await asyncio.gather(response_task, return_exceptions=True)

    async def _receive_terminal_result(
        self, client: Any, runtime: TermTriageRuntimeSummary,
    ) -> sdk.ResultMessage | None:
        """Receive a terminal SDK message for metrics without accepting semantic output from it."""

        terminal: sdk.ResultMessage | None = None
        async for message in client.receive_response():
            self._record_sdk_event(
                runtime, message, timestamp=self._utc_clock(),
                monotonic_now=self._monotonic_clock(),
            )
            if isinstance(message, sdk.ResultMessage):
                terminal = message
        if terminal is None:
            return None
        self._accumulate_usage(runtime, terminal)
        runtime.sdk_terminal_reason = terminal.terminal_reason or terminal.stop_reason
        return terminal

    def _validate_structured_output(
        self, task: TermTriageTask, structured: Any,
        runtime: TermTriageRuntimeSummary,
    ) -> tuple[TermTriageResult | None, bool]:
        """Validate one proposed result and retain only bounded value-free failure facts."""

        self._capture_output_shape(runtime, structured)
        if structured is None:
            runtime.failure_stage = "structured_output_missing"
            return None, False
        try:
            result = TermTriageResult.model_validate(structured)
        except ValidationError as exc:
            runtime.failure_stage = "pydantic_schema_validation"
            runtime.pydantic_errors = self._safe_pydantic_errors(exc)
            correctable = all(
                not detail["location"].split(".", 1)[0] in TRUSTED_IDENTITY_FIELDS
                for detail in runtime.pydantic_errors
            )
            return None, bool(runtime.pydantic_errors) and correctable
        if (
            result.organization_id, result.document_id, result.preprocessing_run_id, result.analysis_run_id,
        ) != (
            task.organization_id, task.document_id, task.preprocessing_run_id, task.analysis_run_id,
        ):
            runtime.failure_stage = "trusted_identity_mismatch"
            runtime.validation_issue_codes = ("identity_or_version_mismatch",)
            return None, False
        issues = validate_term_triage_result(task, result)
        if issues:
            runtime.validation_issue_codes = tuple(issue.code for issue in issues[:50])
            runtime.validation_issues = tuple(
                {"code": issue.code[:64], "location": self._safe_location(issue.location)}
                for issue in issues[:50]
            )
            runtime.failure_stage = "completion_gate_failure"
            return None, all(issue.correctable for issue in issues)
        runtime.failure_stage = None
        runtime.pydantic_errors = ()
        runtime.validation_issue_codes = ()
        runtime.validation_issues = ()
        return result, False

    @staticmethod
    def _safe_pydantic_errors(exc: ValidationError) -> tuple[dict[str, str], ...]:
        """Retain only sanitized locations and stable Pydantic error types."""

        details: list[dict[str, str]] = []
        for error in exc.errors(include_input=False, include_url=False)[:50]:
            location_parts = []
            for part in error.get("loc", ())[:8]:
                value = str(part)
                location_parts.append(value if value.replace("_", "").replace("-", "").isalnum() and len(value) <= 64 else "field")
            error_type = ClaudeTermTriageAgent._map_pydantic_error(error)
            details.append({"location": ".".join(location_parts) or "result", "type": error_type})
        return tuple(details)

    @staticmethod
    def _map_pydantic_error(error: dict[str, Any]) -> str:
        """Map known root-validator failures to safe stable diagnostic codes."""

        text = str(error.get("msg", "")).lower()
        context = error.get("ctx", {})
        if isinstance(context, dict):
            text = f"{text} {context.get('error', '')}".lower()
        mappings = (
            ("duplicate_identifier", ("unique and ordered",)),
        )
        for code, needles in mappings:
            if any(needle in text for needle in needles):
                return code
        return str(error.get("type", "validation_error"))[:64]

    @staticmethod
    def _safe_location(location: str) -> str:
        """Bound an issue location to identifier-safe characters without field values."""

        return location[:200] if location and all(character.isalnum() or character in "._:-[]" for character in location[:200]) else "result"

    @staticmethod
    def _capture_output_shape(runtime: TermTriageRuntimeSummary, structured: Any) -> None:
        """Record expected top-level field names and collection counts without values."""

        runtime.structured_output_present = structured is not None
        if not isinstance(structured, dict):
            runtime.output_field_names = ()
            runtime.output_collection_counts = {}
            return
        expected = set(TermTriageResult.model_fields)
        runtime.output_field_names = tuple(sorted(str(name) for name in structured if name in expected))
        runtime.output_collection_counts = {
            name: len(structured[name])
            for name in OUTPUT_COLLECTION_NAMES
            if isinstance(structured.get(name), (list, tuple))
        }

    @staticmethod
    def _finalizer_input_schema() -> dict[str, Any]:
        """Derive a semantic-only submission schema with trusted fields removed.

        Unlike `document_analysis`/`sku_mapping`/`term_applicability`'s
        finalizers, no evidence transform is needed here at all -- triage
        decisions carry no evidence field to begin with.
        """

        result_schema = copy.deepcopy(TermTriageResult.model_json_schema())
        definitions = result_schema.pop("$defs", None)
        properties = result_schema.get("properties", {})
        required = result_schema.get("required", [])
        for field_name in TRUSTED_IDENTITY_FIELDS:
            properties.pop(field_name, None)
            if field_name in required:
                required.remove(field_name)
        schema = _schema({"result": result_schema}, ["result"])
        # Pydantic references definitions from the schema root, not its nested
        # ``result`` property; preserve them at the finalizer input root.
        if definitions:
            schema["$defs"] = definitions
        return schema

    @staticmethod
    def _trusted_result_fields(task: TermTriageTask) -> dict[str, Any]:
        """Return only application-owned result identity and version values."""

        return {
            "schema_version": TERM_TRIAGE_RESULT_SCHEMA_VERSION,
            "organization_id": str(task.organization_id),
            "document_id": str(task.document_id),
            "preprocessing_run_id": str(task.preprocessing_run_id),
            "analysis_run_id": str(task.analysis_run_id),
        }

    @staticmethod
    def _safe_finalizer_issues(runtime: TermTriageRuntimeSummary) -> list[dict[str, str]]:
        """Return only bounded issue codes and locations to the correction call."""

        issues = [
            {"code": item["type"], "location": item["location"]}
            for item in runtime.pydantic_errors[:50]
        ]
        if not issues:
            issues = [
                {"code": item["code"], "location": item["location"]}
                for item in runtime.validation_issues[:50]
            ]
        return issues or [{"code": "invalid_finalization_input", "location": "result"}]

    @staticmethod
    def _accumulate_usage(runtime: TermTriageRuntimeSummary, message: sdk.ResultMessage) -> None:
        """Accumulate token classes and per-model usage while trusting SDK total cost."""

        usage = message.usage or {}
        def add(current: int | None, *keys: str) -> int | None:
            value = next((usage.get(key) for key in keys if isinstance(usage.get(key), int)), None)
            return current if value is None else (current or 0) + value
        runtime.input_tokens = add(runtime.input_tokens, "input_tokens", "inputTokens")
        runtime.cache_creation_input_tokens = add(runtime.cache_creation_input_tokens, "cache_creation_input_tokens", "cacheCreationInputTokens")
        runtime.cache_read_input_tokens = add(runtime.cache_read_input_tokens, "cache_read_input_tokens", "cacheReadInputTokens")
        runtime.output_tokens = add(runtime.output_tokens, "output_tokens", "outputTokens")
        runtime.turn_count += message.num_turns
        if message.total_cost_usd is not None:
            runtime.cost_usd = message.total_cost_usd
        for model, values in (message.model_usage or {}).items():
            if not isinstance(model, str) or len(model) > 100 or not isinstance(values, dict):
                continue
            safe = runtime.model_usage.setdefault(model, {})
            for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "webSearchRequests"):
                value = values.get(key)
                if isinstance(value, int):
                    safe[key] = int(safe.get(key, 0)) + value
            if isinstance(values.get("costUSD"), (int, float)):
                safe["costUSD"] = float(safe.get("costUSD", 0.0)) + float(values["costUSD"])

    @staticmethod
    async def _configured_mcp_tool_names(options: sdk.ClaudeAgentOptions) -> tuple[str, ...]:
        """List registered tools from the configured in-process SDK server before connection."""

        try:
            configuration = options.mcp_servers[MCP_SERVER_NAME]
            if not isinstance(configuration, dict) or configuration.get("type") != "sdk":
                return ()
            server = configuration.get("instance")
            entry = getattr(server, "_request_handlers", {}).get("tools/list")
            handler = getattr(entry, "handler", None)
            if handler is None:
                return ()
            result = await handler(None, None)
            return tuple(sorted(tool.name for tool in result.tools))
        except Exception:
            return ()

    @staticmethod
    async def _confirm_mcp_ready(client: Any, runtime: TermTriageRuntimeSummary) -> None:
        """Record SDK MCP status, treating an idle absent in-process server as not reported."""

        try:
            status = await client.get_mcp_status()
        except Exception:
            raise TermTriageRuntimeError("term_triage_mcp_initialization_failed", runtime=runtime) from None
        servers = status.get("mcpServers", ()) if isinstance(status, dict) else ()
        server = next((item for item in servers if item.get("name") == MCP_SERVER_NAME), None)
        if server is None:
            runtime.mcp_server_status = "not_reported"
            return
        if server.get("status") == "failed":
            raise TermTriageRuntimeError("term_triage_mcp_initialization_failed", runtime=runtime)
        if server.get("status") == "connected":
            discovered = tuple(sorted(item.get("name") for item in server.get("tools", ()) if item.get("name")))
            if discovered != tuple(sorted(TOOL_NAMES)):
                raise TermTriageRuntimeError("term_triage_mcp_tool_discovery_failed", runtime=runtime)
            runtime.mcp_server_status = "connected"
            runtime.announced_tool_names = discovered
            return
        raise TermTriageRuntimeError("term_triage_mcp_initialization_failed", runtime=runtime)

    @staticmethod
    def _prompt(task: TermTriageTask) -> str:
        """Build compact instructions embedding the fixed task's own candidate/term summaries."""

        candidates = [
            {"candidate_id": item.candidate_id, "raw_name": item.raw_name, "commercial_status": item.commercial_status.value}
            for item in task.candidates
        ]
        terms = [
            {"term_id": item.term_id, "raw_name": item.raw_name, "raw_value": item.raw_value}
            for item in task.terms
        ]
        return (
            "Classify every term in one fixed batch from one completed document analysis. Follow the term-triage skill. "
            "You have no evidence tools and make no applicability claim; classify only each term's rough shape from its own raw name/value and the candidate list below. "
            "Classify every term exactly once as document_metadata, potential_line_item, or uncertain. "
            "Prefer uncertain over forcing a confident document_metadata classification when a term could plausibly relate to a purchased line item -- "
            "in particular, treat any term touching service period, billing or invoicing frequency, payment terms, currency, quantity, pricing, discount, tax, renewal, or commitment/order dates conservatively rather than assuming it is administrative. "
            "Do not infer applicability scope, candidate links, purchase treatment, or normalized values -- that is not this stage's job. "
            "Keep rationale concise; do not write long explanations. "
            "When finished, call finalize_term_triage exactly once with the complete batch of decisions; it is the only completion mechanism. "
            "Do not return a final answer before that call. Submit promptly once every term has a decision. Reserve enough time for one correction. If it returns safe validation issues, correct only those issues and submit once more when allowed. "
            f"candidates={json.dumps(candidates, sort_keys=True, default=str)}; "
            f"terms={json.dumps(terms, sort_keys=True, default=str)}."
        )

    @staticmethod
    def _record_sdk_event(
        runtime: TermTriageRuntimeSummary,
        message: Any,
        *,
        timestamp: datetime | None = None,
        monotonic_now: float | None = None,
    ) -> None:
        """Retain timestamped SDK event types without recording event content or payloads."""

        timestamp = timestamp or datetime.now(UTC)
        monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
        event_types = set(runtime.tool_event_types)
        event_type = type(message).__name__
        event_types.add(event_type)
        runtime.record_timing_event(
            kind="sdk_event_received", timestamp=timestamp,
            monotonic_now=monotonic_now, event_type=event_type,
        )
        if isinstance(message, sdk.AssistantMessage):
            for block in message.content:
                if isinstance(block, sdk.ToolUseBlock):
                    runtime._next_sdk_tool_sequence += 1
                    name = str(block.name)[:100]
                    allowed = name in MCP_TOOL_NAMES
                    if len(runtime.sdk_tool_use_events) < MAX_RUNTIME_TOOL_DIAGNOSTICS:
                        runtime.sdk_tool_use_events.append({
                            "tool_name": name,
                            "sequence_number": runtime._next_sdk_tool_sequence,
                            "timestamp": timestamp.isoformat(),
                            "elapsed_ms": runtime.elapsed_ms(monotonic_now),
                            "allowed": allowed,
                            "tool_kind": "finalizer" if allowed else "unavailable",
                        })
                    else:
                        runtime.tool_diagnostics_truncated = True
                    event_types.add("ToolUseBlock")
        if isinstance(message, sdk.SystemMessage):
            subtype = getattr(message, "subtype", None)
            category = getattr(message, "category", None)
            safe_category = category or subtype
            if isinstance(safe_category, str) and safe_category:
                runtime.sdk_system_categories = tuple(sorted(set(runtime.sdk_system_categories) | {safe_category[:100]}))
        runtime.tool_event_types = tuple(sorted(event_types))

    @staticmethod
    def _terminal_code(reason: str | None, message: Any) -> str:
        """Map recognized SDK terminal conditions to stable safe errors."""
        if getattr(message, "api_error_status", None) in {401, 403}: return "term_triage_authentication_failed"
        return {"max_turns": "term_triage_max_turns", "timeout": "term_triage_timeout", "budget": "term_triage_budget_exceeded"}.get(reason or "", "term_triage_runtime_failed")

    def _build_adapters(self, task: TermTriageTask):
        """Create exactly one in-memory guarded finalizer -- no read-only tools."""
        runtime = TermTriageRuntimeSummary(
            request_id=uuid.uuid4(), started_at=self._utc_clock(),
            monotonic_started_at=self._monotonic_clock(),
        )
        capture = _FinalizationCapture()
        setattr(runtime, "_finalization_capture", capture)

        @sdk.tool(FINALIZER_TOOL_NAME, "Submit one validated batch of term-triage decisions and stop.", self._finalizer_input_schema())
        async def finalize(args):
            sequence_number = runtime.begin_tool_invocation(
                FINALIZER_TOOL_NAME, self._utc_clock(), self._monotonic_clock(),
            )
            runtime.finalization_submission_count += 1
            schema_validated = False
            try:
                if set(args) != {"result"} or not isinstance(args.get("result"), dict):
                    raise ValueError("invalid finalization input")
                submitted = args["result"]
                schema_validated = True
                if TRUSTED_IDENTITY_FIELDS.intersection(submitted):
                    runtime.failure_stage = "trusted_identity_mismatch"
                    runtime.validation_issue_codes = ("trusted_identity_override",)
                    runtime.validation_issues = (
                        {"code": "trusted_identity_override", "location": "result"},
                    )
                    result, correctable = None, False
                else:
                    proposed = {**submitted, **self._trusted_result_fields(task)}
                    result, correctable = self._validate_structured_output(
                        task, proposed, runtime,
                    )
                if result is not None:
                    capture.accepted_result = result
                    runtime.finalization_accepted = True
                    runtime.tool_call_count += 1
                    runtime.tool_calls_by_name[FINALIZER_TOOL_NAME] = runtime.tool_calls_by_name.get(FINALIZER_TOOL_NAME, 0) + 1
                    runtime.successful_tool_call_order = (*runtime.successful_tool_call_order, FINALIZER_TOOL_NAME)
                    runtime.finish_tool_invocation(
                        sequence_number, FINALIZER_TOOL_NAME, succeeded=True,
                        timestamp=self._utc_clock(), monotonic_now=self._monotonic_clock(),
                    )
                    runtime.record_adapter_invocation(FINALIZER_TOOL_NAME, schema_validated=True, succeeded=True)
                    capture.accepted_event.set()
                    return {"content": [{"type": "text", "text": "Result accepted. Stop now."}]}

                runtime.failed_tool_call_count += 1
                runtime.failed_tool_calls_by_name[FINALIZER_TOOL_NAME] = runtime.failed_tool_calls_by_name.get(FINALIZER_TOOL_NAME, 0) + 1
                runtime.finish_tool_invocation(
                    sequence_number, FINALIZER_TOOL_NAME, succeeded=False,
                    timestamp=self._utc_clock(), monotonic_now=self._monotonic_clock(),
                )
                runtime.record_adapter_invocation(
                    FINALIZER_TOOL_NAME, schema_validated=schema_validated,
                    succeeded=False,
                    failure_category=(
                        "schema_rejected"
                        if runtime.failure_stage == "pydantic_schema_validation"
                        else "validation_rejected"
                    ),
                )
                correction_allowed = (
                    correctable
                    and not capture.correctable_rejection_seen
                    and self._settings.term_triage_max_corrections > 0
                )
                if correction_allowed:
                    capture.correctable_rejection_seen = True
                    runtime.correction_attempt_count = 1
                else:
                    capture.rejection_event.set()
                return {
                    "content": [{"type": "text", "text": json.dumps({
                        "issues": self._safe_finalizer_issues(runtime),
                        "correction_allowed": correction_allowed,
                    }, sort_keys=True, separators=(",", ":"))}],
                    "is_error": True,
                }
            except (ValidationError, ValueError, TypeError):
                runtime.failure_stage = "pydantic_schema_validation"
                runtime.pydantic_errors = ({"location": "result", "type": "invalid_input"},)
                runtime.failed_tool_call_count += 1
                runtime.failed_tool_calls_by_name[FINALIZER_TOOL_NAME] = runtime.failed_tool_calls_by_name.get(FINALIZER_TOOL_NAME, 0) + 1
                runtime.finish_tool_invocation(
                    sequence_number, FINALIZER_TOOL_NAME, succeeded=False,
                    timestamp=self._utc_clock(), monotonic_now=self._monotonic_clock(),
                )
                runtime.record_adapter_invocation(
                    FINALIZER_TOOL_NAME, schema_validated=schema_validated,
                    succeeded=False,
                    failure_category="schema_rejected",
                )
                capture.rejection_event.set()
                return {"content": [{"type": "text", "text": json.dumps({"issues": self._safe_finalizer_issues(runtime), "correction_allowed": False}, sort_keys=True)}], "is_error": True}
            except BaseException:
                runtime.finish_tool_invocation(
                    sequence_number, FINALIZER_TOOL_NAME, succeeded=False,
                    timestamp=self._utc_clock(), monotonic_now=self._monotonic_clock(),
                )
                runtime.record_adapter_invocation(FINALIZER_TOOL_NAME, schema_validated=schema_validated, succeeded=False, failure_category="cancelled")
                raise
        return [finalize], runtime


def _schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    """Build a closed JSON Schema for one operation-only Claude tool input."""
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


__all__ = [
    "ClaudeTermTriageAgent",
    "TermTriageAgent",
    "TermTriageExecution",
    "TermTriageRuntimeSummary",
    "UnconfiguredTermTriageAgent",
]
