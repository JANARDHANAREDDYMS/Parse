"""Run a bounded Claude Agent SDK loop over one fixed term-applicability task.

The concrete agent receives one `TermApplicabilityTask` (a whole completed
analysis run's candidates and terms, batched) and a typed toolset, and
returns a validated `TermApplicabilityResult` plus small operational
metrics. It never persists decisions, never connects to workers, and never
exposes arbitrary files, SQL, or unrestricted tools.

Claude receives only the compact task context in the prompt and two narrow,
task-bound evidence tools — there is no `search_document`-style tool and no
generic unrestricted evidence tool: `get_evidence_region` stays an internal
dependency of `PersistedTermApplicabilityTools`, never a Claude-visible tool
in its own right. Evidence is always submitted by ID and resolved server-side
(`_resolve_decision_evidence_ids`) against the task's own trusted
`evidence_by_id` map; Claude is never asked to reconstruct a bounding box,
representation, or extraction source.
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
from maximor.term_applicability.contracts import TermApplicabilityTask, TermApplicabilityToolset
from maximor.term_applicability.errors import (
    TermApplicabilityConfigurationError,
    TermApplicabilityNotConfiguredError,
    TermApplicabilityRuntimeError,
    TermApplicabilityToolError,
    TermApplicabilityValidationError,
)
from maximor.term_applicability.schemas import (
    CandidateCommercialFactCoverage,
    RawCommercialFactField,
    TermApplicabilityResult,
    compute_fact_id,
)
from maximor.term_applicability.tool_schemas import CandidateEvidenceRegionInput, TermEvidenceRegionInput
from maximor.term_applicability.validation import validate_term_applicability_result
from maximor.term_applicability.versions import (
    TERM_APPLICABILITY_DECISION_SCHEMA_VERSION,
    TERM_APPLICABILITY_RESULT_SCHEMA_VERSION,
)

MAX_RUNTIME_TIMING_EVENTS = 200
MAX_RUNTIME_TOOL_DIAGNOSTICS = 100
FINALIZER_RESULT_GRACE_SECONDS = 2.0

READ_ONLY_TOOL_NAMES = ("get_term_evidence_region", "get_candidate_evidence_region")
FINALIZER_TOOL_NAME = "finalize_term_applicability"
TOOL_NAMES = (*READ_ONLY_TOOL_NAMES, FINALIZER_TOOL_NAME)
MCP_SERVER_NAME = "maximor_term_applicability"
MCP_TOOL_NAMES = tuple(f"mcp__{MCP_SERVER_NAME}__{name}" for name in TOOL_NAMES)
OUTPUT_COLLECTION_NAMES = ("decisions", "candidate_commercial_facts")
TRUSTED_IDENTITY_FIELDS = {"schema_version", "organization_id", "document_id", "preprocessing_run_id", "analysis_run_id"}


class TermApplicabilityAgent(Protocol):
    """Resolve document/candidate/unknown scope for every term in one fixed task."""

    async def resolve(
        self,
        task: TermApplicabilityTask,
        tools: TermApplicabilityToolset,
    ) -> TermApplicabilityResult:
        """Return a validated applicability result without persisting anything."""

        ...


class UnconfiguredTermApplicabilityAgent:
    """Fail explicitly until a concrete tool-using agent implementation is configured."""

    async def resolve(
        self,
        task: TermApplicabilityTask,
        tools: TermApplicabilityToolset,
    ) -> TermApplicabilityResult:
        """Raise a typed failure and never synthesize a fake successful result."""

        del task, tools
        raise TermApplicabilityNotConfiguredError


@dataclass
class _FinalizationCapture:
    """Keep one request-scoped accepted result in memory without persistence."""

    accepted_event: asyncio.Event = field(default_factory=asyncio.Event)
    rejection_event: asyncio.Event = field(default_factory=asyncio.Event)
    accepted_result: TermApplicabilityResult | None = None
    correctable_rejection_seen: bool = False


@dataclass
class TermApplicabilityRuntimeSummary:
    """Keep bounded operational facts for one invocation, never document bodies or secrets.

    This mirrors `DocumentAnalysisRuntimeSummary`/`SkuMappingRuntimeSummary`
    field-for-field where the concept is shared (session/tool timing,
    finalizer status, token/cost, bounded event arrays) and omits fields with
    no analogue here (there is no per-invocation catalog snapshot to pin, and
    no retrieval shortlist to retain). Every duration is measured against
    `monotonic_started_at` (a monotonic clock), never wall-clock subtraction;
    every timestamp recorded for correlation is UTC. Nothing here observes or
    claims to observe hidden model reasoning: `elapsed_after_last_successful_tool_return_ms`
    is an *observed interval* with no tool activity in it, not a measurement
    of "thinking time" — the model could be doing anything, including nothing
    yet, in that interval.
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
    referenced_term_ids: tuple[str, ...] = ()
    referenced_candidate_ids: tuple[str, ...] = ()
    retrieved_term_evidence_ids: dict[str, frozenset[str]] = field(default_factory=dict)
    retrieved_candidate_evidence_ids: dict[str, frozenset[str]] = field(default_factory=dict)
    candidate_commercial_fact_counts_by_field: dict[str, int] = field(default_factory=dict)
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

    def record_term_evidence_retrieved(self, term_id: str, evidence_id: str) -> None:
        """Record one successful term-scoped evidence retrieval for the completion gate."""

        self.retrieved_term_evidence_ids[term_id] = self.retrieved_term_evidence_ids.get(term_id, frozenset()) | {evidence_id}

    def record_candidate_evidence_retrieved(self, candidate_id: str, evidence_id: str) -> None:
        """Record one successful candidate-scoped evidence retrieval for the completion gate."""

        self.retrieved_candidate_evidence_ids[candidate_id] = self.retrieved_candidate_evidence_ids.get(candidate_id, frozenset()) | {evidence_id}

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
        """Record total elapsed time and post-tool elapsed time without attributing cause.

        `elapsed_after_last_successful_tool_return_ms` is an observed
        interval, not a measurement of model reasoning: nothing here can see
        inside the model, so this never claims to.
        """

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
            "terms_referenced": list(self.referenced_term_ids[:100]),
            "candidates_referenced": list(self.referenced_candidate_ids[:100]),
            "retrieved_term_evidence_ids": {key: sorted(value) for key, value in list(self.retrieved_term_evidence_ids.items())[:100]},
            "retrieved_candidate_evidence_ids": {key: sorted(value) for key, value in list(self.retrieved_candidate_evidence_ids.items())[:100]},
            "candidate_commercial_fact_counts_by_field": dict(self.candidate_commercial_fact_counts_by_field),
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
class TermApplicabilityExecution:
    """Return a validated result and non-semantic runtime summary together."""

    result: TermApplicabilityResult
    runtime: TermApplicabilityRuntimeSummary


class ClaudeTermApplicabilityAgent:
    """Use Claude through two read-only tools and one in-memory submission tool.

    Claude receives the fixed task's compact candidate/term summaries
    directly in the prompt (not through a tool call) and operation-only tool
    arguments; trusted identity and version fields are captured from the
    task, never from Claude. This class performs no persistence, scheduling,
    or worker integration.
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

    def build_options(self, task: TermApplicabilityTask, tools: TermApplicabilityToolset) -> sdk.ClaudeAgentOptions:
        """Build isolated SDK options with two readers and one guarded finalizer."""
        key = self._settings.anthropic_api_key
        if key is None or not key.get_secret_value():
            raise TermApplicabilityConfigurationError
        adapters, runtime = self._build_adapters(task, tools)
        server = sdk.create_sdk_mcp_server(MCP_SERVER_NAME, tools=adapters)
        options = sdk.ClaudeAgentOptions(
            tools=[], allowed_tools=list(MCP_TOOL_NAMES), disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch"],
            mcp_servers={MCP_SERVER_NAME: server}, strict_mcp_config=True,
            model=self._settings.term_applicability_model, max_turns=self._settings.term_applicability_max_turns,
            max_thinking_tokens=self._settings.term_applicability_max_thinking_tokens,
            max_budget_usd=self._settings.term_applicability_max_budget_usd,
            cwd=self._settings.term_applicability_project_root.resolve(), settings=None,
            setting_sources=["project"], skills=["term-applicability"], plugins=[],
            env={"ANTHROPIC_API_KEY": key.get_secret_value()},
        )
        runtime.mcp_server_status = "configured"
        runtime.setting_sources = tuple(options.setting_sources or ())
        object.__setattr__(options, "_maximor_runtime", runtime)
        object.__setattr__(options, "_maximor_adapters", adapters)
        return options

    async def resolve(self, task: TermApplicabilityTask, tools: TermApplicabilityToolset) -> TermApplicabilityResult:
        """Run applicability resolution and return the validated result required by the agent protocol."""
        return (await self.execute(task, tools)).result

    async def execute(
        self,
        task: TermApplicabilityTask,
        tools: TermApplicabilityToolset,
    ) -> TermApplicabilityExecution:
        """Run the bounded SDK loop and return a result with bounded runtime metrics."""
        options = self.build_options(task, tools)
        runtime = getattr(options, "_maximor_runtime")
        result: TermApplicabilityResult | None = None
        client: Any | None = None
        stage = "initialization"
        try:
            async with asyncio.timeout(self._settings.term_applicability_timeout_seconds):
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
                    raise TermApplicabilityRuntimeError("term_applicability_mcp_tool_discovery_failed", runtime=runtime)
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
            raise TermApplicabilityRuntimeError("term_applicability_timeout", runtime=runtime) from None
        except TermApplicabilityRuntimeError:
            runtime.record_terminal("failure", self._utc_clock(), self._monotonic_clock())
            raise
        except TermApplicabilityValidationError:
            runtime.record_terminal("validation_failure", self._utc_clock(), self._monotonic_clock())
            raise
        except asyncio.CancelledError:
            runtime.terminal_reason = "cancelled"
            runtime.record_terminal("cancelled", self._utc_clock(), self._monotonic_clock())
            raise
        except Exception:
            code = "term_applicability_sdk_initialization_failed" if stage == "initialization" else "term_applicability_sdk_transport_failed"
            runtime.record_terminal("failure", self._utc_clock(), self._monotonic_clock())
            raise TermApplicabilityRuntimeError(code, runtime=runtime) from None
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
            raise TermApplicabilityValidationError(runtime=runtime)
        return TermApplicabilityExecution(result=result, runtime=runtime)

    async def _await_finalization(
        self, client: Any, runtime: TermApplicabilityRuntimeSummary,
        capture: _FinalizationCapture,
    ) -> TermApplicabilityResult:
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
                raise TermApplicabilityValidationError(runtime=runtime)
            terminal = await response_task
            if terminal is None:
                runtime.failure_stage = "finalizer_not_called"
                raise TermApplicabilityRuntimeError(
                    "term_applicability_finalizer_not_called", runtime=runtime,
                )
            if terminal.is_error or runtime.sdk_terminal_reason in {
                "max_turns", "timeout", "budget", "aborted_streaming", "aborted_tools",
            }:
                raise TermApplicabilityRuntimeError(
                    self._terminal_code(runtime.sdk_terminal_reason, terminal), runtime=runtime,
                )
            runtime.failure_stage = "finalizer_not_called"
            raise TermApplicabilityRuntimeError(
                "term_applicability_finalizer_not_called", runtime=runtime,
            )
        finally:
            for task in (accepted_task, rejected_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(accepted_task, rejected_task, return_exceptions=True)

    async def _complete_accepted_finalization(
        self, client: Any, runtime: TermApplicabilityRuntimeSummary,
        capture: _FinalizationCapture, response_task: asyncio.Task,
    ) -> TermApplicabilityResult:
        """Return an accepted result after a short optional terminal-message grace period."""

        result = capture.accepted_result
        if result is None:
            raise TermApplicabilityValidationError(runtime=runtime)
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
        self, client: Any, runtime: TermApplicabilityRuntimeSummary,
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
        self, task: TermApplicabilityTask, structured: Any,
        runtime: TermApplicabilityRuntimeSummary,
    ) -> tuple[TermApplicabilityResult | None, bool]:
        """Validate one proposed result and retain only bounded value-free failure facts."""

        self._capture_output_shape(runtime, structured)
        if structured is None:
            runtime.failure_stage = "structured_output_missing"
            return None, False
        try:
            result = TermApplicabilityResult.model_validate(structured)
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
        issues = validate_term_applicability_result(task, result, runtime)
        if issues:
            runtime.validation_issue_codes = tuple(issue.code for issue in issues[:50])
            runtime.validation_issues = tuple(
                {"code": issue.code[:64], "location": self._safe_location(issue.location)}
                for issue in issues[:50]
            )
            evidence_codes = {"evidence_not_grounded_in_task", "evidence_not_retrieved", "candidate_evidence_not_retrieved"}
            runtime.failure_stage = "evidence_validation_failure" if any(issue.code in evidence_codes for issue in issues) else "completion_gate_failure"
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
            error_type = ClaudeTermApplicabilityAgent._map_pydantic_error(error)
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
            ("candidate_ids_not_canonical", ("unique and lexically ordered",)),
            ("line_item_missing_scope", ("line_item disposition requires an applicability_scope",)),
            ("candidate_scope_missing_candidates", ("candidate scope requires at least one candidate id",)),
            ("non_candidate_scope_carries_candidates", ("document or unknown scope must not name candidate ids",)),
            ("line_item_missing_evidence", ("line_item applicability decision requires at least one evidence reference",)),
            ("metadata_carries_scope", ("document_metadata must not carry an applicability_scope",)),
            ("metadata_carries_candidates", ("document_metadata must not carry candidate ids",)),
            ("fact_id_not_canonical", ("fact_id must be the canonical identifier",)),
            ("fact_candidate_id_mismatch", ("every fact must belong to this bundle's own candidate_id",)),
            ("duplicate_fact_id_in_bundle", ("facts must not repeat the same fact_id",)),
            ("duplicate_or_unordered_candidate_facts", ("candidate_commercial_facts must be unique and ordered",)),
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
    def _capture_output_shape(runtime: TermApplicabilityRuntimeSummary, structured: Any) -> None:
        """Record expected top-level field names and collection counts without values."""

        runtime.structured_output_present = structured is not None
        if not isinstance(structured, dict):
            runtime.output_field_names = ()
            runtime.output_collection_counts = {}
            return
        expected = set(TermApplicabilityResult.model_fields)
        runtime.output_field_names = tuple(sorted(str(name) for name in structured if name in expected))
        runtime.output_collection_counts = {
            name: len(structured[name])
            for name in OUTPUT_COLLECTION_NAMES
            if isinstance(structured.get(name), (list, tuple))
        }

    @staticmethod
    def _finalizer_input_schema() -> dict[str, Any]:
        """Derive a semantic-only submission schema with trusted fields removed.

        Each decision's `evidence` is replaced with `evidence_ids` (plain
        `block_id`/`table_id` strings) inside the nested `TermApplicabilityDecision`
        definition, and each decision's own `schema_version` is stripped the
        same way the result-level trusted fields are — both are injected
        server-side (`_resolve_decision_evidence_ids`), never supplied by
        Claude. This makes reconstructing a structured `EvidenceReference`
        (representation, extraction source, bounding box) unnecessary and
        structurally impossible to get subtly wrong, mirroring
        `sku_mapping`'s identical finalizer-schema design.

        Each `RawCommercialFact`'s `evidence` is stripped the same way, and
        its `fact_id` is removed entirely: `fact_id` is always the
        server-computed canonical identifier (`compute_fact_id`), never a
        value Claude proposes, exactly like the trusted result-level fields.
        """

        result_schema = copy.deepcopy(TermApplicabilityResult.model_json_schema())
        definitions = result_schema.pop("$defs", None) or {}
        properties = result_schema.get("properties", {})
        required = result_schema.get("required", [])
        for field_name in TRUSTED_IDENTITY_FIELDS:
            properties.pop(field_name, None)
            if field_name in required:
                required.remove(field_name)

        decision_definition = definitions.get("TermApplicabilityDecision")
        if decision_definition is not None:
            decision_properties = decision_definition.get("properties", {})
            decision_required = decision_definition.get("required", [])
            decision_properties.pop("schema_version", None)
            if "schema_version" in decision_required:
                decision_required.remove("schema_version")
            decision_properties.pop("evidence", None)
            if "evidence" in decision_required:
                decision_required.remove("evidence")
            decision_properties["evidence_ids"] = {
                "type": "array", "items": {"type": "string"},
                "description": "block_id or table_id values taken from this term's or the named candidate's own listed evidence.",
            }

        fact_definition = definitions.get("RawCommercialFact")
        if fact_definition is not None:
            fact_properties = fact_definition.get("properties", {})
            fact_required = fact_definition.get("required", [])
            fact_properties.pop("fact_id", None)
            if "fact_id" in fact_required:
                fact_required.remove("fact_id")
            fact_properties.pop("evidence", None)
            if "evidence" in fact_required:
                fact_required.remove("evidence")
            fact_properties["evidence_ids"] = {
                "type": "array", "items": {"type": "string"},
                "description": "block_id or table_id values taken from this fact's own candidate's listed evidence.",
            }

        coverage_definition = definitions.get("CandidateCommercialFactCoverage")
        if coverage_definition is not None:
            coverage_properties = coverage_definition.get("properties", {})
            coverage_required = coverage_definition.get("required", [])
            coverage_properties.pop("expected_fields", None)
            if "expected_fields" in coverage_required:
                coverage_required.remove("expected_fields")
            # `extracted_fields` is never submitted: the application already
            # holds this same submission's own evidence-resolved
            # `candidate_commercial_facts` and derives the extracted set
            # from them directly (see `_resolve_coverage_evidence_ids`).
            # Restating it was redundant and let a model's own summary
            # silently drift from its own facts.
            coverage_properties.pop("extracted_fields", None)
            if "extracted_fields" in coverage_required:
                coverage_required.remove("extracted_fields")
            coverage_properties.pop("evidence", None)
            if "evidence" in coverage_required:
                coverage_required.remove("evidence")
            coverage_properties["evidence_ids"] = {
                "type": "array", "items": {"type": "string"},
                "description": "Candidate evidence IDs successfully retrieved during this run.",
            }

        schema = _schema({"result": result_schema}, ["result"])
        # Pydantic references definitions from the schema root, not its nested
        # ``result`` property; preserve them at the finalizer input root.
        if definitions:
            schema["$defs"] = definitions
        return schema

    @staticmethod
    def _resolve_decision_evidence_ids(task: TermApplicabilityTask, decisions: Any) -> list[dict[str, Any]] | None:
        """Resolve every submitted decision's `evidence_ids` to the task's own exact evidence.

        Also injects each decision's trusted `schema_version` here, in the
        same pass, since both are server-owned and Claude never supplies
        either. Returns `None` on any unresolvable identifier, malformed
        decision, or a decision that already carries a trusted field, so the
        caller can reject the whole submission as a correctable content
        mistake, never a partial or best-effort result.
        """

        if not isinstance(decisions, list):
            return None
        resolved: list[dict[str, Any]] = []
        for item in decisions:
            if not isinstance(item, dict) or "schema_version" in item:
                return None
            evidence_ids = item.get("evidence_ids", [])
            if not isinstance(evidence_ids, list) or not all(isinstance(value, str) for value in evidence_ids):
                return None
            references: list[dict[str, Any]] = []
            for identifier in evidence_ids:
                reference = task.evidence_by_id.get(identifier)
                if reference is None:
                    return None
                references.append(reference.model_dump(mode="json"))
            resolved_item = {key: value for key, value in item.items() if key != "evidence_ids"}
            resolved_item["evidence"] = references
            resolved_item["schema_version"] = TERM_APPLICABILITY_DECISION_SCHEMA_VERSION
            resolved.append(resolved_item)
        return resolved

    @staticmethod
    def _resolve_candidate_facts_evidence_ids(task: TermApplicabilityTask, bundles: Any) -> list[dict[str, Any]] | None:
        """Resolve every submitted fact's `evidence_ids` and inject its trusted `fact_id`.

        Mirrors `_resolve_decision_evidence_ids`: Claude submits a candidate
        ID, field name, raw value, raw period context, and bare evidence-ID
        strings per fact; the application resolves those IDs against the
        task's own trusted `evidence_by_id` map and computes the canonical
        `fact_id` server-side via `compute_fact_id`, never trusting an
        identifier Claude supplies. Returns `None` on any unresolvable
        evidence identifier, malformed bundle or fact, unrecognized field
        name, or a fact that already carries a `fact_id`, so the caller can
        reject the whole submission as a correctable content mistake, never
        a partial or best-effort result.
        """

        if not isinstance(bundles, list):
            return None
        resolved_bundles: list[dict[str, Any]] = []
        for bundle in bundles:
            if not isinstance(bundle, dict):
                return None
            candidate_id = bundle.get("candidate_id")
            facts = bundle.get("facts", [])
            if not isinstance(candidate_id, str) or not isinstance(facts, list):
                return None
            resolved_facts: list[dict[str, Any]] = []
            for item in facts:
                if not isinstance(item, dict) or "fact_id" in item:
                    return None
                field_raw = item.get("field")
                raw_value = item.get("raw_value")
                fact_candidate_id = item.get("candidate_id")
                if not isinstance(field_raw, str) or not isinstance(raw_value, str) or not isinstance(fact_candidate_id, str):
                    return None
                for optional_key in ("raw_period_label", "raw_period_start", "raw_period_end"):
                    optional_value = item.get(optional_key)
                    if optional_value is not None and not isinstance(optional_value, str):
                        return None
                evidence_ids = item.get("evidence_ids", [])
                if not isinstance(evidence_ids, list) or not all(isinstance(value, str) for value in evidence_ids):
                    return None
                references: list[dict[str, Any]] = []
                for identifier in evidence_ids:
                    reference = task.evidence_by_id.get(identifier)
                    if reference is None:
                        return None
                    references.append(reference.model_dump(mode="json"))
                try:
                    field = RawCommercialFactField(field_raw)
                except ValueError:
                    return None
                resolved_item = {key: value for key, value in item.items() if key != "evidence_ids"}
                resolved_item["evidence"] = references
                resolved_item["fact_id"] = compute_fact_id(
                    candidate_id=fact_candidate_id, field=field, raw_value=raw_value,
                    raw_period_label=item.get("raw_period_label"),
                    raw_period_start=item.get("raw_period_start"),
                    raw_period_end=item.get("raw_period_end"),
                )
                resolved_facts.append(resolved_item)
            resolved_bundles.append({"candidate_id": candidate_id, "facts": resolved_facts})
        return resolved_bundles

    @staticmethod
    def _resolve_coverage_evidence_ids(
        task: TermApplicabilityTask, coverage_items: Any, resolved_facts: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        """Resolve coverage evidence IDs and derive expected/extracted fields server-side.

        Claude submits only `candidate_id`, `unresolved_fields`, and
        evidence IDs. `expected_fields` was already application-owned
        (derived from the task's own raw-attribute hints); `extracted_fields`
        is now derived the same way, as the sorted set of `field` values
        already present in this *same submission's* own evidence-resolved
        `resolved_facts` for that candidate -- never restated by Claude, and
        therefore structurally unable to drift from the facts actually
        submitted (the exact failure mode `candidate_fact_coverage_extracted_mismatch`
        used to catch after the fact).
        """

        if not isinstance(coverage_items, list):
            return None
        by_id = {candidate.candidate_id: candidate for candidate in task.candidates}
        facts_by_candidate: dict[str, set[str]] = {}
        for bundle in resolved_facts:
            if not isinstance(bundle, dict):
                continue
            bundle_candidate_id = bundle.get("candidate_id")
            for fact in bundle.get("facts", []):
                if isinstance(fact, dict) and isinstance(fact.get("field"), str):
                    facts_by_candidate.setdefault(bundle_candidate_id, set()).add(fact["field"])
        resolved: list[dict[str, Any]] = []
        for item in coverage_items:
            if not isinstance(item, dict) or "expected_fields" in item or "extracted_fields" in item or "evidence" in item:
                return None
            candidate_id = item.get("candidate_id")
            candidate = by_id.get(candidate_id)
            if candidate is None:
                return None
            evidence_ids = item.get("evidence_ids", [])
            if not isinstance(evidence_ids, list) or not all(isinstance(value, str) for value in evidence_ids):
                return None
            references = []
            for identifier in evidence_ids:
                reference = task.evidence_by_id.get(identifier)
                if reference is None:
                    return None
                references.append(reference.model_dump(mode="json"))
            unresolved = item.get("unresolved_fields", [])
            if not isinstance(unresolved, list):
                return None
            resolved.append({
                "candidate_id": candidate_id,
                "expected_fields": [field.value for field in candidate.expected_fact_fields],
                "extracted_fields": sorted(facts_by_candidate.get(candidate_id, ())),
                "unresolved_fields": unresolved,
                "evidence": references,
            })
        return resolved

    @staticmethod
    def _record_candidate_fact_diagnostics(runtime: TermApplicabilityRuntimeSummary, result: TermApplicabilityResult) -> None:
        """Record bounded fact counts by field, never raw fact values."""

        counts: dict[str, int] = {}
        for bundle in result.candidate_commercial_facts:
            for fact in bundle.facts:
                counts[fact.field.value] = counts.get(fact.field.value, 0) + 1
        runtime.candidate_commercial_fact_counts_by_field = counts

    @staticmethod
    def _trusted_result_fields(task: TermApplicabilityTask) -> dict[str, Any]:
        """Return only application-owned result identity and version values."""

        return {
            "schema_version": TERM_APPLICABILITY_RESULT_SCHEMA_VERSION,
            "organization_id": str(task.organization_id),
            "document_id": str(task.document_id),
            "preprocessing_run_id": str(task.preprocessing_run_id),
            "analysis_run_id": str(task.analysis_run_id),
        }

    @staticmethod
    def _safe_finalizer_issues(runtime: TermApplicabilityRuntimeSummary) -> list[dict[str, str]]:
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
    def _accumulate_usage(runtime: TermApplicabilityRuntimeSummary, message: sdk.ResultMessage) -> None:
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
    async def _confirm_mcp_ready(client: Any, runtime: TermApplicabilityRuntimeSummary) -> None:
        """Record SDK MCP status, treating an idle absent in-process server as not reported."""

        try:
            status = await client.get_mcp_status()
        except Exception:
            raise TermApplicabilityRuntimeError("term_applicability_mcp_initialization_failed", runtime=runtime) from None
        servers = status.get("mcpServers", ()) if isinstance(status, dict) else ()
        server = next((item for item in servers if item.get("name") == MCP_SERVER_NAME), None)
        if server is None:
            runtime.mcp_server_status = "not_reported"
            return
        if server.get("status") == "failed":
            raise TermApplicabilityRuntimeError("term_applicability_mcp_initialization_failed", runtime=runtime)
        if server.get("status") == "connected":
            discovered = tuple(sorted(item.get("name") for item in server.get("tools", ()) if item.get("name")))
            if discovered != tuple(sorted(TOOL_NAMES)):
                raise TermApplicabilityRuntimeError("term_applicability_mcp_tool_discovery_failed", runtime=runtime)
            runtime.mcp_server_status = "connected"
            runtime.announced_tool_names = discovered
            return
        raise TermApplicabilityRuntimeError("term_applicability_mcp_initialization_failed", runtime=runtime)

    @staticmethod
    def _prompt(task: TermApplicabilityTask) -> str:
        """Build compact instructions embedding the fixed task's own candidate/term summaries.

        Like `sku_mapping`'s prompt (compact content embedded directly) and
        unlike `document_analysis`'s (identifiers only, content behind
        tools): the whole batch is already bounded and known, so it is
        embedded directly rather than requiring a tool call to "discover" it.

        Only `task.effective_selected_term_ids` are rendered here -- a term
        triage did not select is never shown to this agent at all, which is
        the actual mechanism that narrows this agent's reasoning burden, not
        just a validation-time filter.
        """

        candidates = [
            {
                "candidate_id": item.candidate_id, "raw_name": item.raw_name,
                "commercial_status": item.commercial_status.value,
                "raw_attributes": dict(item.raw_attributes),
                "expected_fact_fields": [field.value for field in item.expected_fact_fields],
                "evidence_ids": list(item.evidence_ids),
            }
            for item in task.candidates
        ]
        selected = set(task.effective_selected_term_ids)
        terms = [
            {"term_id": item.term_id, "raw_name": item.raw_name, "raw_value": item.raw_value, "evidence_ids": list(item.evidence_ids)}
            for item in task.terms if item.term_id in selected
        ]
        return (
            "Resolve term applicability for one fixed batch of candidates and terms from one completed document analysis, "
            "and separately extract raw commercial facts for existing eligible candidates. "
            "Follow the term-applicability skill. "
            "The terms below are already the selected subset a prior triage pass judged worth attribution; you do not need to classify anything outside this batch. "
            "Start from the compact context below; do not compare every term against every candidate. "
            "Before deciding document or candidate scope for a term, call get_term_evidence_region for that term at least once; unknown scope does not require it. "
            "Before naming any candidate in a candidate-scope decision, call get_candidate_evidence_region for each named candidate at least once. "
            "Use unknown scope when attribution is not directly supported by evidence -- unknown is a preferred, correct outcome, not a fallback to avoid. "
            "An evidence ID in task context is not usable until its successful evidence-tool retrieval occurs in this run; complete all required retrievals before the first finalizer call. "
            "Affirmative document/candidate scope requires retrieved term evidence, candidate scope additionally requires retrieved evidence for every named candidate, and every raw commercial fact requires retrieved evidence for its own candidate. "
            "If evidence was not retrieved, omit the fact or claim, or use unknown rather than citing it. "
            "Do not select a SKU, normalize dates or money, calculate amounts, or reconsider commercial status. "
            "Cite evidence_ids as the exact block/table identifiers already listed below, never a full evidence object and never an identifier not listed here. "
            "Submit exactly one decision for every selected term; use unknown when evidence cannot support attribution. "
            "Separately, for each candidate below whose commercial_status is purchased or included only -- never excluded, optional, mentioned, or ambiguous, and never a candidate you would invent -- "
            "extract raw commercial facts restricted to: quantity, currency, unit_price, unit_price_period, total_listed_value, service_start_date, service_end_date, "
            "invoicing_schedule_type, invoicing_frequency, payment_terms, special_note, yearly_price. "
            "Before asserting any fact for a candidate, call get_candidate_evidence_region for that candidate and cite only its own evidence_ids listed below. "
            "Preserve every fact value exactly as raw text from the document; never normalize a date or currency, never calculate a total, unit price, or quantity, and never derive a missing value. "
            "Represent a multi-year or multi-period price (e.g. Year 1, Year 2, Year 3) as separate yearly_price facts distinguished by raw_period_label, never summed or averaged into one value. "
            "Omit a fact entirely when no evidence supports it for that candidate -- never invent a default, zero, or placeholder value; a term shared across candidates that cannot be attributed to one is not a fact. "
            "Submit facts inside candidate_commercial_facts, grouped by candidate_id, in the same finalize_term_applicability call as your term decisions; it is the only completion mechanism. "
            "For every eligible candidate with expected_fact_fields, submit exactly one candidate_commercial_fact_coverage declaration: list only the fields you could not extract in unresolved_fields, retrieve candidate evidence before declaring a field unresolved, and never fabricate a value. "
            "Do not restate which fields you did extract -- the application derives extracted_fields directly from your own submitted candidate_commercial_facts, so summarizing it yourself is unnecessary and must be omitted from your submission. "
            "Do not return a final answer before that call. Reserve enough time for one correction. If it returns safe validation issues, correct only those issues and submit once more when allowed. "
            f"candidates={json.dumps(candidates, sort_keys=True, default=str)}; "
            f"terms={json.dumps(terms, sort_keys=True, default=str)}."
        )

    @staticmethod
    def _record_sdk_event(
        runtime: TermApplicabilityRuntimeSummary,
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
                    short_name = name.removeprefix(f"mcp__{MCP_SERVER_NAME}__")
                    if allowed:
                        kind = "finalizer" if short_name == FINALIZER_TOOL_NAME else "retrieval"
                    else:
                        kind = "unavailable"
                    if len(runtime.sdk_tool_use_events) < MAX_RUNTIME_TOOL_DIAGNOSTICS:
                        runtime.sdk_tool_use_events.append({
                            "tool_name": name,
                            "sequence_number": runtime._next_sdk_tool_sequence,
                            "timestamp": timestamp.isoformat(),
                            "elapsed_ms": runtime.elapsed_ms(monotonic_now),
                            "allowed": allowed,
                            "tool_kind": kind,
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
        if getattr(message, "api_error_status", None) in {401, 403}: return "term_applicability_authentication_failed"
        return {"max_turns": "term_applicability_max_turns", "timeout": "term_applicability_timeout", "budget": "term_applicability_budget_exceeded"}.get(reason or "", "term_applicability_runtime_failed")

    def _build_adapters(self, task: TermApplicabilityTask, tools: TermApplicabilityToolset):
        """Create two read-only adapters and one in-memory guarded finalizer."""
        runtime = TermApplicabilityRuntimeSummary(
            request_id=uuid.uuid4(), started_at=self._utc_clock(),
            monotonic_started_at=self._monotonic_clock(),
        )
        capture = _FinalizationCapture()
        setattr(runtime, "_finalization_capture", capture)
        async def invoke(name: str, model, args: dict[str, Any], trusted_kwargs: dict[str, Any] | None = None):
            sequence_number = runtime.begin_tool_invocation(
                name, self._utc_clock(), self._monotonic_clock(),
            )
            schema_validated = False
            try:
                if trusted_kwargs and set(trusted_kwargs).intersection(args):
                    raise ValueError("trusted scope cannot be overridden")
                value = model(**(trusted_kwargs or {}), **args)
                schema_validated = True
                result = await getattr(tools, name)(value)
                if name == "get_term_evidence_region":
                    runtime.record_term_evidence_retrieved(value.term_id, value.evidence_id)
                    runtime.referenced_term_ids = tuple(sorted({*runtime.referenced_term_ids, value.term_id}))
                elif name == "get_candidate_evidence_region":
                    runtime.record_candidate_evidence_retrieved(value.candidate_id, value.evidence_id)
                    runtime.referenced_candidate_ids = tuple(sorted({*runtime.referenced_candidate_ids, value.candidate_id}))
                runtime.tool_call_count += 1
                runtime.tool_calls_by_name[name] = runtime.tool_calls_by_name.get(name, 0) + 1
                runtime.successful_tool_call_order = (*runtime.successful_tool_call_order, name)
                response = {"content": [{"type": "text", "text": json.dumps(result, default=lambda x: x.model_dump(mode="json") if hasattr(x, "model_dump") else str(x), sort_keys=True)}]}
                runtime.finish_tool_invocation(
                    sequence_number, name, succeeded=True,
                    timestamp=self._utc_clock(), monotonic_now=self._monotonic_clock(),
                )
                runtime.record_adapter_invocation(name, schema_validated=True, succeeded=True)
                return response
            except (TermApplicabilityToolError, ValidationError, ValueError, TypeError) as exc:
                runtime.failed_tool_call_count += 1
                runtime.failed_tool_calls_by_name[name] = runtime.failed_tool_calls_by_name.get(name, 0) + 1
                runtime.tool_event_types = tuple(sorted(set(runtime.tool_event_types) | {f"ToolAdapterError:{exc.__class__.__name__}"}))
                runtime.finish_tool_invocation(
                    sequence_number, name, succeeded=False,
                    timestamp=self._utc_clock(), monotonic_now=self._monotonic_clock(),
                )
                runtime.record_adapter_invocation(
                    name, schema_validated=schema_validated,
                    succeeded=False,
                    failure_category="schema_rejected" if isinstance(exc, (ValidationError, ValueError, TypeError)) else "tool_error",
                )
                return {"content": [{"type": "text", "text": "Tool request could not be completed."}], "is_error": True}
            except Exception as exc:
                runtime.failed_tool_call_count += 1
                runtime.failed_tool_calls_by_name[name] = runtime.failed_tool_calls_by_name.get(name, 0) + 1
                runtime.tool_event_types = tuple(sorted(set(runtime.tool_event_types) | {f"ToolAdapterError:{exc.__class__.__name__}"}))
                runtime.finish_tool_invocation(
                    sequence_number, name, succeeded=False,
                    timestamp=self._utc_clock(), monotonic_now=self._monotonic_clock(),
                )
                runtime.record_adapter_invocation(name, schema_validated=True, succeeded=False, failure_category="adapter_error")
                return {"content": [{"type": "text", "text": "Tool request could not be completed."}], "is_error": True}
            except BaseException:
                runtime.finish_tool_invocation(
                    sequence_number, name, succeeded=False,
                    timestamp=self._utc_clock(), monotonic_now=self._monotonic_clock(),
                )
                runtime.record_adapter_invocation(name, schema_validated=True, succeeded=False, failure_category="cancelled")
                raise

        @sdk.tool("get_term_evidence_region", "Resolve one persisted evidence region already cited by one fixed term.", _schema(
            {"term_id": {"type": "string"}, "evidence_id": {"type": "string"}}, ["term_id", "evidence_id"],
        ))
        async def term_evidence(args): return await invoke(
            "get_term_evidence_region", TermEvidenceRegionInput, args,
            {"organization_id": task.organization_id, "analysis_run_id": task.analysis_run_id},
        )

        @sdk.tool("get_candidate_evidence_region", "Resolve one persisted evidence region already cited by one fixed candidate.", _schema(
            {"candidate_id": {"type": "string"}, "evidence_id": {"type": "string"}}, ["candidate_id", "evidence_id"],
        ))
        async def candidate_evidence(args): return await invoke(
            "get_candidate_evidence_region", CandidateEvidenceRegionInput, args,
            {"organization_id": task.organization_id, "analysis_run_id": task.analysis_run_id},
        )

        @sdk.tool(FINALIZER_TOOL_NAME, "Submit one validated batch of term-applicability decisions and candidate commercial facts, and stop.", self._finalizer_input_schema())
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
                submitted = self._canonicalize_submission(submitted)
                if TRUSTED_IDENTITY_FIELDS.intersection(submitted):
                    runtime.failure_stage = "trusted_identity_mismatch"
                    runtime.validation_issue_codes = ("trusted_identity_override",)
                    runtime.validation_issues = (
                        {"code": "trusted_identity_override", "location": "result"},
                    )
                    result, correctable = None, False
                else:
                    resolved_decisions = self._resolve_decision_evidence_ids(task, submitted.get("decisions", []))
                    resolved_facts = self._resolve_candidate_facts_evidence_ids(
                        task, submitted.get("candidate_commercial_facts", []),
                    )
                    resolved_coverage = self._resolve_coverage_evidence_ids(
                        task, submitted.get("candidate_commercial_fact_coverage", []),
                        resolved_facts or [],
                    )
                    if resolved_decisions is None or resolved_facts is None or resolved_coverage is None:
                        runtime.failure_stage = "evidence_identifier_unresolved"
                        runtime.validation_issue_codes = ("evidence_identifier_unresolved",)
                        runtime.validation_issues = (
                            {
                                "code": "evidence_identifier_unresolved",
                                "location": "result:decisions" if resolved_decisions is None else "result:candidate_commercial_facts",
                            },
                        )
                        result, correctable = None, True
                    else:
                        proposed = {
                            **submitted, "decisions": resolved_decisions,
                            "candidate_commercial_facts": resolved_facts,
                            "candidate_commercial_fact_coverage": resolved_coverage,
                            **self._trusted_result_fields(task),
                        }
                        result, correctable = self._validate_structured_output(
                            task, proposed, runtime,
                        )
                if result is not None:
                    self._record_candidate_fact_diagnostics(runtime, result)
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
                    and self._settings.term_applicability_max_corrections > 0
                )
                if correction_allowed:
                    capture.correctable_rejection_seen = True
                    runtime.correction_attempt_count = 1
                else:
                    capture.rejection_event.set()
                return {
                    "content": [{"type": "text", "text": json.dumps({
                        "issues": self._safe_finalizer_issues(runtime),
                        "correction": self._grounding_correction_payload(task, submitted, runtime),
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
                return {"content": [{"type": "text", "text": json.dumps({"issues": self._safe_finalizer_issues(runtime), "correction": [], "correction_allowed": False}, sort_keys=True)}], "is_error": True}
            except BaseException:
                runtime.finish_tool_invocation(
                    sequence_number, FINALIZER_TOOL_NAME, succeeded=False,
                    timestamp=self._utc_clock(), monotonic_now=self._monotonic_clock(),
                )
                runtime.record_adapter_invocation(FINALIZER_TOOL_NAME, schema_validated=schema_validated, succeeded=False, failure_category="cancelled")
                raise
        return [term_evidence, candidate_evidence, finalize], runtime

    @staticmethod
    def _canonicalize_submission(submitted: Any) -> Any:
        """Canonicalize collection ordering without removing duplicates or repairing values."""

        if not isinstance(submitted, dict):
            return submitted
        normalized = copy.deepcopy(submitted)
        decisions = normalized.get("decisions")
        if isinstance(decisions, list):
            normalized["decisions"] = sorted(
                decisions, key=lambda item: str(item.get("term_id", "")) if isinstance(item, dict) else "",
            )
            for item in normalized["decisions"]:
                if isinstance(item, dict) and isinstance(item.get("evidence_ids"), list):
                    item["evidence_ids"] = sorted(item["evidence_ids"])
        bundles = normalized.get("candidate_commercial_facts")
        if isinstance(bundles, list):
            normalized["candidate_commercial_facts"] = sorted(
                bundles, key=lambda item: str(item.get("candidate_id", "")) if isinstance(item, dict) else "",
            )
            for bundle in normalized["candidate_commercial_facts"]:
                if not isinstance(bundle, dict):
                    continue
                facts = bundle.get("facts")
                if isinstance(facts, list):
                    def fact_key(item: Any) -> str:
                        if not isinstance(item, dict):
                            return ""
                        try:
                            return compute_fact_id(
                                candidate_id=str(item.get("candidate_id", bundle.get("candidate_id", ""))),
                                field=RawCommercialFactField(item.get("field")),
                                raw_value=str(item.get("raw_value", "")),
                                raw_period_label=item.get("raw_period_label"),
                                raw_period_start=item.get("raw_period_start"),
                                raw_period_end=item.get("raw_period_end"),
                            )
                        except (TypeError, ValueError):
                            return ""
                    bundle["facts"] = sorted(facts, key=fact_key)
                    for fact in bundle["facts"]:
                        if isinstance(fact, dict) and isinstance(fact.get("evidence_ids"), list):
                            fact["evidence_ids"] = sorted(fact["evidence_ids"])
        coverage = normalized.get("candidate_commercial_fact_coverage")
        if isinstance(coverage, list):
            normalized["candidate_commercial_fact_coverage"] = sorted(
                coverage, key=lambda item: str(item.get("candidate_id", "")) if isinstance(item, dict) else "",
            )
            for item in normalized["candidate_commercial_fact_coverage"]:
                if isinstance(item, dict):
                    # `extracted_fields` is never part of Claude's own
                    # submission (see `_resolve_coverage_evidence_ids`); only
                    # `unresolved_fields`/`evidence_ids` need canonical order here.
                    for key in ("unresolved_fields", "evidence_ids"):
                        if isinstance(item.get(key), list):
                            item[key] = sorted(item[key])
        return normalized

    @staticmethod
    def _grounding_correction_payload(
        task: TermApplicabilityTask, submitted: Any, runtime: TermApplicabilityRuntimeSummary,
    ) -> list[dict[str, Any]]:
        """Build bounded grounding feedback scoped to the affected term or candidate."""

        feedback: list[dict[str, Any]] = []
        if not isinstance(submitted, dict):
            return feedback
        term_allowed = runtime.retrieved_term_evidence_ids
        candidate_allowed = runtime.retrieved_candidate_evidence_ids
        for index, decision in enumerate(submitted.get("decisions", [])):
            if not isinstance(decision, dict):
                continue
            term_id = decision.get("term_id")
            ids = decision.get("evidence_ids", [])
            if isinstance(term_id, str) and isinstance(ids, list):
                missing = [value for value in ids if isinstance(value, str) and value not in term_allowed.get(term_id, frozenset())]
                if missing:
                    feedback.append({"location": f"decisions.{index}.evidence_ids", "requirement": "term_evidence", "allowed_evidence_ids": sorted(term_allowed.get(term_id, frozenset())), "instruction": "Replace unsupported IDs with allowed retrieved IDs, omit the unsupported conclusion, or use unknown."})
            if decision.get("applicability_scope") == "candidate":
                for candidate_id in decision.get("applies_to_candidate_ids", []):
                    if isinstance(candidate_id, str) and not candidate_allowed.get(candidate_id):
                        feedback.append({"location": f"decisions.{index}.applies_to_candidate_ids", "requirement": "candidate_evidence", "allowed_evidence_ids": sorted(candidate_allowed.get(candidate_id, frozenset())), "instruction": "Retrieve and cite this candidate's evidence, or remove the candidate association and use unknown."})
        for bundle_index, bundle in enumerate(submitted.get("candidate_commercial_facts", [])):
            if not isinstance(bundle, dict):
                continue
            candidate_id = bundle.get("candidate_id")
            ids = sorted(candidate_allowed.get(candidate_id, frozenset())) if isinstance(candidate_id, str) else []
            submitted_ids = [eid for fact in bundle.get("facts", []) if isinstance(fact, dict) for eid in fact.get("evidence_ids", []) if isinstance(eid, str)]
            if candidate_id and any(eid not in candidate_allowed.get(candidate_id, frozenset()) for eid in submitted_ids):
                feedback.append({"location": f"candidate_commercial_facts.{bundle_index}.facts", "requirement": "commercial_fact_evidence", "allowed_evidence_ids": ids, "instruction": "Use only this candidate's retrieved evidence, or omit unsupported facts."})
        return feedback[:20]


def _schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    """Build a closed JSON Schema for one operation-only Claude tool input."""
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


__all__ = [
    "ClaudeTermApplicabilityAgent",
    "TermApplicabilityAgent",
    "TermApplicabilityExecution",
    "TermApplicabilityRuntimeSummary",
    "UnconfiguredTermApplicabilityAgent",
]
