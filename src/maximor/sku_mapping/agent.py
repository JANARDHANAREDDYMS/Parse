"""Run a bounded Claude Agent SDK loop over one fixed SKU-mapping task.

The concrete agent receives one task and a typed toolset, returns a validated
decision plus small operational metrics, and does not persist decisions or
connect to workers. It never exposes arbitrary files, SQL, or unrestricted
tools. Unlike `document_analysis`'s request (identifiers only, content
discovered through tools), `SkuMappingTask` already carries its candidate's
compact content — so the prompt below includes that content directly rather
than requiring a tool call to discover it.
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
from maximor.document_analysis.tool_schemas import EvidenceRegionInput
from maximor.sku_mapping.contracts import SkuMappingTask, SkuMappingToolset
from maximor.sku_mapping.errors import (
    SkuMappingConfigurationError,
    SkuMappingNotConfiguredError,
    SkuMappingRuntimeError,
    SkuMappingToolError,
    SkuMappingValidationError,
)
from maximor.sku_mapping.schemas import MAX_RETRIEVED_SKUS, SkuMappingDecision, SkuRecord, SkuRetrievalResult
from maximor.sku_mapping.tool_schemas import GetAuthoritativeSkuInput, RetrieveSkusInput
from maximor.sku_mapping.validation import validate_sku_mapping_decision


class SkuMappingAgent(Protocol):
    """Decide MATCH/NO_MATCH/AMBIGUOUS for one fixed candidate through typed narrow tools."""

    async def decide(
        self,
        task: SkuMappingTask,
        tools: SkuMappingToolset,
    ) -> SkuMappingDecision:
        """Return a validated mapping decision without persisting anything."""

        ...


class UnconfiguredSkuMappingAgent:
    """Fail explicitly until a concrete tool-using agent implementation is configured."""

    async def decide(
        self,
        task: SkuMappingTask,
        tools: SkuMappingToolset,
    ) -> SkuMappingDecision:
        """Raise a typed failure and never synthesize a fake successful decision."""

        del task, tools
        raise SkuMappingNotConfiguredError


READ_ONLY_TOOL_NAMES = ("retrieve_skus", "get_authoritative_sku", "get_evidence_region")
FINALIZER_TOOL_NAME = "submit_sku_mapping_decision"
TOOL_NAMES = (*READ_ONLY_TOOL_NAMES, FINALIZER_TOOL_NAME)
MCP_SERVER_NAME = "maximor_sku_mapping"
MCP_TOOL_NAMES = tuple(f"mcp__{MCP_SERVER_NAME}__{name}" for name in TOOL_NAMES)
OUTPUT_COLLECTION_NAMES = ("considered_sku_ids", "evidence")
TRUSTED_IDENTITY_FIELDS = {
    "schema_version", "organization_id", "document_id", "preprocessing_run_id",
    "analysis_run_id", "candidate_id", "catalog_version_id",
}
MAX_RUNTIME_TIMING_EVENTS = 200
MAX_RUNTIME_TOOL_DIAGNOSTICS = 100
FINALIZER_RESULT_GRACE_SECONDS = 2.0


@dataclass
class _FinalizationCapture:
    """Keep one request-scoped accepted decision in memory without persistence."""

    accepted_event: asyncio.Event = field(default_factory=asyncio.Event)
    rejection_event: asyncio.Event = field(default_factory=asyncio.Event)
    accepted_result: SkuMappingDecision | None = None
    correctable_rejection_seen: bool = False


@dataclass
class SkuMappingRuntimeSummary:
    """Keep bounded operational facts for one invocation, never document bodies or secrets.

    `catalog_version_id`, `authoritative_skus_by_id`, and
    `last_retrieval_result` have no equivalent in
    `DocumentAnalysisRuntimeSummary`: the first two exist because
    `retrieve_skus` and `get_authoritative_sku` must stay pinned to one
    catalog snapshot within an invocation, and because
    `validate_sku_mapping_decision` cross-checks a proposed `MATCH` against
    whatever this runtime's own successful `get_authoritative_sku` calls
    actually confirmed, rather than re-querying the database itself.
    `last_retrieval_result` retains the most recent successful `retrieve_skus`
    response so a persistence layer can bundle the retrieved shortlist
    alongside the decision in one canonical artifact, without a second
    retrieval call.
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
    referenced_pages: tuple[int, ...] = ()
    referenced_evidence_ids: tuple[str, ...] = ()
    catalog_version_id: uuid.UUID | None = None
    authoritative_skus_by_id: dict[uuid.UUID, SkuRecord] = field(default_factory=dict)
    last_retrieval_result: SkuRetrievalResult | None = None
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
            "successful_retrieval_count": self.tool_calls_by_name.get("retrieve_skus", 0),
            "successful_authoritative_lookup_count": self.tool_calls_by_name.get("get_authoritative_sku", 0),
            "catalog_version_id": str(self.catalog_version_id) if self.catalog_version_id else None,
            "confirmed_sku_count": len(self.authoritative_skus_by_id),
            "pages_accessed": list(self.referenced_pages[:100]),
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
class SkuMappingExecution:
    """Return a validated decision and non-semantic runtime summary together."""

    decision: SkuMappingDecision
    runtime: SkuMappingRuntimeSummary


class ClaudeSkuMappingAgent:
    """Use Claude through three read-only tools and one in-memory submission tool.

    Claude receives the fixed candidate's compact content directly in the
    prompt (not through a tool call) and operation-only tool arguments; trusted
    identity and catalog-version scope are captured from the task and the
    runtime's own successful `retrieve_skus` call. This class performs no
    persistence, scheduling, or catalog administration.
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

    def build_options(self, task: SkuMappingTask, tools: SkuMappingToolset) -> sdk.ClaudeAgentOptions:
        """Build isolated SDK options with three readers and one guarded finalizer."""
        key = self._settings.anthropic_api_key
        if key is None or not key.get_secret_value():
            raise SkuMappingConfigurationError
        adapters, runtime = self._build_adapters(task, tools)
        server = sdk.create_sdk_mcp_server(MCP_SERVER_NAME, tools=adapters)
        options = sdk.ClaudeAgentOptions(
            tools=[], allowed_tools=list(MCP_TOOL_NAMES), disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch"],
            mcp_servers={MCP_SERVER_NAME: server}, strict_mcp_config=True,
            model=self._settings.sku_mapping_model, max_turns=self._settings.sku_mapping_max_turns,
            max_thinking_tokens=self._settings.sku_mapping_max_thinking_tokens,
            max_budget_usd=self._settings.sku_mapping_max_budget_usd,
            cwd=self._settings.sku_mapping_project_root.resolve(), settings=None,
            setting_sources=["project"], skills=["sku-mapping"], plugins=[],
            env={"ANTHROPIC_API_KEY": key.get_secret_value()},
        )
        runtime.mcp_server_status = "configured"
        runtime.setting_sources = tuple(options.setting_sources or ())
        object.__setattr__(options, "_maximor_runtime", runtime)
        object.__setattr__(options, "_maximor_adapters", adapters)
        return options

    async def decide(self, task: SkuMappingTask, tools: SkuMappingToolset) -> SkuMappingDecision:
        """Run mapping and return the validated decision required by the agent protocol."""
        return (await self.execute(task, tools)).decision

    async def execute(
        self,
        task: SkuMappingTask,
        tools: SkuMappingToolset,
    ) -> SkuMappingExecution:
        """Run the bounded SDK loop and return a decision with bounded runtime metrics."""
        options = self.build_options(task, tools)
        runtime = getattr(options, "_maximor_runtime")
        result: SkuMappingDecision | None = None
        client: Any | None = None
        stage = "initialization"
        try:
            async with asyncio.timeout(self._settings.sku_mapping_timeout_seconds):
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
                    raise SkuMappingRuntimeError("sku_mapping_mcp_tool_discovery_failed", runtime=runtime)
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
            raise SkuMappingRuntimeError("sku_mapping_timeout", runtime=runtime) from None
        except SkuMappingRuntimeError:
            runtime.record_terminal("failure", self._utc_clock(), self._monotonic_clock())
            raise
        except SkuMappingValidationError:
            runtime.record_terminal("validation_failure", self._utc_clock(), self._monotonic_clock())
            raise
        except asyncio.CancelledError:
            runtime.terminal_reason = "cancelled"
            runtime.record_terminal("cancelled", self._utc_clock(), self._monotonic_clock())
            raise
        except Exception:
            code = "sku_mapping_sdk_initialization_failed" if stage == "initialization" else "sku_mapping_sdk_transport_failed"
            runtime.record_terminal("failure", self._utc_clock(), self._monotonic_clock())
            raise SkuMappingRuntimeError(code, runtime=runtime) from None
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
            raise SkuMappingValidationError(runtime=runtime)
        return SkuMappingExecution(decision=result, runtime=runtime)

    async def _await_finalization(
        self, client: Any, runtime: SkuMappingRuntimeSummary,
        capture: _FinalizationCapture,
    ) -> SkuMappingDecision:
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
                raise SkuMappingValidationError(runtime=runtime)
            terminal = await response_task
            if terminal is None:
                runtime.failure_stage = "finalizer_not_called"
                raise SkuMappingRuntimeError(
                    "sku_mapping_finalizer_not_called", runtime=runtime,
                )
            if terminal.is_error or runtime.sdk_terminal_reason in {
                "max_turns", "timeout", "budget", "aborted_streaming", "aborted_tools",
            }:
                raise SkuMappingRuntimeError(
                    self._terminal_code(runtime.sdk_terminal_reason, terminal), runtime=runtime,
                )
            runtime.failure_stage = "finalizer_not_called"
            raise SkuMappingRuntimeError(
                "sku_mapping_finalizer_not_called", runtime=runtime,
            )
        finally:
            for task in (accepted_task, rejected_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(accepted_task, rejected_task, return_exceptions=True)

    async def _complete_accepted_finalization(
        self, client: Any, runtime: SkuMappingRuntimeSummary,
        capture: _FinalizationCapture, response_task: asyncio.Task,
    ) -> SkuMappingDecision:
        """Return an accepted decision after a short optional terminal-message grace period."""

        result = capture.accepted_result
        if result is None:
            raise SkuMappingValidationError(runtime=runtime)
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
        self, client: Any, runtime: SkuMappingRuntimeSummary,
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
        self, task: SkuMappingTask, structured: Any,
        runtime: SkuMappingRuntimeSummary,
    ) -> tuple[SkuMappingDecision | None, bool]:
        """Validate one proposed decision and retain only bounded value-free failure facts."""

        self._capture_output_shape(runtime, structured)
        if structured is None:
            runtime.failure_stage = "structured_output_missing"
            return None, False
        try:
            decision = SkuMappingDecision.model_validate(structured)
        except ValidationError as exc:
            runtime.failure_stage = "pydantic_schema_validation"
            runtime.pydantic_errors = self._safe_pydantic_errors(exc)
            correctable = all(
                not detail["location"].split(".", 1)[0] in TRUSTED_IDENTITY_FIELDS
                for detail in runtime.pydantic_errors
            )
            return None, bool(runtime.pydantic_errors) and correctable
        if (decision.organization_id, decision.document_id, decision.preprocessing_run_id, decision.analysis_run_id, decision.candidate_id) != (task.organization_id, task.document_id, task.preprocessing_run_id, task.analysis_run_id, task.candidate_id):
            runtime.failure_stage = "trusted_identity_mismatch"
            runtime.validation_issue_codes = ("identity_or_version_mismatch",)
            return None, False
        issues = validate_sku_mapping_decision(task, decision, runtime)
        if issues:
            runtime.validation_issue_codes = tuple(issue.code for issue in issues[:50])
            runtime.validation_issues = tuple(
                {"code": issue.code[:64], "location": self._safe_location(issue.location)}
                for issue in issues[:50]
            )
            runtime.failure_stage = (
                "evidence_validation_failure"
                if any(issue.code == "evidence_not_grounded_in_task" for issue in issues)
                else "completion_gate_failure"
            )
            return None, all(issue.correctable for issue in issues)
        runtime.failure_stage = None
        runtime.pydantic_errors = ()
        runtime.validation_issue_codes = ()
        runtime.validation_issues = ()
        return decision, False

    @staticmethod
    def _safe_pydantic_errors(exc: ValidationError) -> tuple[dict[str, str], ...]:
        """Retain only sanitized locations and stable Pydantic error types."""

        details: list[dict[str, str]] = []
        for error in exc.errors(include_input=False, include_url=False)[:50]:
            location_parts = []
            for part in error.get("loc", ())[:8]:
                value = str(part)
                location_parts.append(value if value.replace("_", "").replace("-", "").isalnum() and len(value) <= 64 else "field")
            error_type = ClaudeSkuMappingAgent._map_pydantic_error(error)
            details.append({"location": ".".join(location_parts) or "decision", "type": error_type})
        return tuple(details)

    @staticmethod
    def _map_pydantic_error(error: dict[str, Any]) -> str:
        """Map known root-validator failures to safe stable diagnostic codes."""

        text = str(error.get("msg", "")).lower()
        context = error.get("ctx", {})
        if isinstance(context, dict):
            text = f"{text} {context.get('error', '')}".lower()
        mappings = (
            ("match_missing_sku_fields", ("requires sku_id, sku_code, and sku_name",)),
            ("non_match_carries_sku_fields", ("only a match decision may carry",)),
            ("ambiguous_missing_considered_skus", ("at least two considered skus",)),
            ("considered_skus_duplicated", ("must not repeat",)),
            ("non_ambiguous_carries_considered_skus", ("only an ambiguous decision may carry",)),
            ("decision_missing_evidence", ("requires at least one evidence reference",)),
            ("decision_evidence_run_mismatch", ("evidence must belong to the decision's preprocessing run",)),
        )
        for code, needles in mappings:
            if any(needle in text for needle in needles):
                return code
        return str(error.get("type", "validation_error"))[:64]

    @staticmethod
    def _safe_location(location: str) -> str:
        """Bound an issue location to identifier-safe characters without field values."""

        return location[:200] if location and all(character.isalnum() or character in "._:-[]" for character in location[:200]) else "decision"

    @staticmethod
    def _capture_output_shape(runtime: SkuMappingRuntimeSummary, structured: Any) -> None:
        """Record expected top-level field names and collection counts without values."""

        runtime.structured_output_present = structured is not None
        if not isinstance(structured, dict):
            runtime.output_field_names = ()
            runtime.output_collection_counts = {}
            return
        expected = set(SkuMappingDecision.model_fields)
        runtime.output_field_names = tuple(sorted(str(name) for name in structured if name in expected))
        runtime.output_collection_counts = {
            name: len(structured[name])
            for name in OUTPUT_COLLECTION_NAMES
            if isinstance(structured.get(name), (list, tuple))
        }

    @staticmethod
    def _finalizer_input_schema() -> dict[str, Any]:
        """Derive a semantic-only submission schema with trusted fields removed.

        `evidence` is replaced with `evidence_ids` (plain `block_id`/`table_id`
        strings). A live run against Claude showed the structured
        `EvidenceReference` shape (`representation`, `extraction_source`,
        `bounding_box`, ...) cannot be reliably reconstructed from the compact
        facts in the prompt, causing correct evidence citations to fail an
        exact-equality check against the task's own evidence. Citing by
        identifier and resolving server-side (`_resolve_evidence_ids`) makes
        that whole failure class structurally impossible instead of relying
        on the model to reproduce a nested object byte-for-byte.
        """

        result_schema = copy.deepcopy(SkuMappingDecision.model_json_schema())
        definitions = result_schema.pop("$defs", None)
        properties = result_schema.get("properties", {})
        required = result_schema.get("required", [])
        for field_name in TRUSTED_IDENTITY_FIELDS:
            properties.pop(field_name, None)
            if field_name in required:
                required.remove(field_name)
        properties.pop("evidence", None)
        if "evidence" in required:
            required.remove("evidence")
        properties["evidence_ids"] = {
            "type": "array", "items": {"type": "string"}, "minItems": 1,
            "description": "block_id or table_id values taken from the candidate's own listed evidence.",
        }
        required.append("evidence_ids")
        schema = _schema({"decision": result_schema}, ["decision"])
        # Pydantic references definitions from the schema root, not its nested
        # ``decision`` property; preserve them at the finalizer input root.
        if definitions:
            schema["$defs"] = definitions
        return schema

    @staticmethod
    def _resolve_evidence_ids(
        task: SkuMappingTask, evidence_ids: Any, runtime: "SkuMappingRuntimeSummary | None" = None,
    ) -> list[dict[str, Any]] | None:
        """Resolve submitted block_id/table_id identifiers to the task's own exact evidence.

        Returns `None` on any unresolvable or malformed identifier so the
        caller can reject the submission as a correctable content mistake,
        never a partial or best-effort evidence list. When `runtime` is
        supplied and the identifiers are well-typed but one or more don't
        match this candidate's own evidence, records exactly which
        identifiers failed and which ones were actually available -- a bare
        `evidence_identifier_unresolved` code left the model's one
        correction with no way to tell a genuine typo from citing a block
        that belongs to a different candidate.
        """

        if runtime is not None:
            runtime.validation_issues = ()
        if not isinstance(evidence_ids, list) or not evidence_ids or not all(isinstance(item, str) for item in evidence_ids):
            return None
        by_identifier = {(reference.block_id or reference.table_id): reference for reference in task.all_evidence}
        unresolved = [identifier for identifier in evidence_ids if identifier not in by_identifier]
        if unresolved:
            if runtime is not None:
                runtime.validation_issues = (
                    {
                        "code": "evidence_identifier_unresolved", "location": "decision:evidence_ids",
                        "unresolved_ids": ",".join(sorted(set(unresolved))[:20]),
                        "allowed_ids": ",".join(sorted(by_identifier)[:50]),
                    },
                )
            return None
        return [by_identifier[identifier].model_dump(mode="json") for identifier in evidence_ids]

    @staticmethod
    def _trusted_result_fields(task: SkuMappingTask, runtime: SkuMappingRuntimeSummary) -> dict[str, Any]:
        """Return only application-owned decision identity and catalog-version values.

        `catalog_version_id` comes from the runtime, not the task: it is only
        known once this invocation's own `retrieve_skus` call has actually
        run, so the finalizer can never accept a decision scoped to a
        different catalog snapshot than the one this invocation retrieved.
        """

        return {
            "schema_version": task.schema_version,
            "organization_id": str(task.organization_id),
            "document_id": str(task.document_id),
            "preprocessing_run_id": str(task.preprocessing_run_id),
            "analysis_run_id": str(task.analysis_run_id),
            "candidate_id": task.candidate_id,
            "catalog_version_id": str(runtime.catalog_version_id) if runtime.catalog_version_id else None,
        }

    @staticmethod
    def _safe_finalizer_issues(runtime: SkuMappingRuntimeSummary) -> list[dict[str, str]]:
        """Return only bounded issue codes and locations to the correction call."""

        issues = [
            {"code": item["type"], "location": item["location"]}
            for item in runtime.pydantic_errors[:50]
        ]
        if not issues:
            issues = [dict(item) for item in runtime.validation_issues[:50]]
        return issues or [{"code": "invalid_finalization_input", "location": "decision"}]

    @staticmethod
    def _accumulate_usage(runtime: SkuMappingRuntimeSummary, message: sdk.ResultMessage) -> None:
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
    async def _confirm_mcp_ready(client: Any, runtime: SkuMappingRuntimeSummary) -> None:
        """Record SDK MCP status, treating an idle absent in-process server as not reported."""

        try:
            status = await client.get_mcp_status()
        except Exception:
            raise SkuMappingRuntimeError("sku_mapping_mcp_initialization_failed", runtime=runtime) from None
        servers = status.get("mcpServers", ()) if isinstance(status, dict) else ()
        server = next((item for item in servers if item.get("name") == MCP_SERVER_NAME), None)
        if server is None:
            runtime.mcp_server_status = "not_reported"
            return
        if server.get("status") == "failed":
            raise SkuMappingRuntimeError("sku_mapping_mcp_initialization_failed", runtime=runtime)
        if server.get("status") == "connected":
            discovered = tuple(sorted(item.get("name") for item in server.get("tools", ()) if item.get("name")))
            if discovered != tuple(sorted(TOOL_NAMES)):
                raise SkuMappingRuntimeError("sku_mapping_mcp_tool_discovery_failed", runtime=runtime)
            runtime.mcp_server_status = "connected"
            runtime.announced_tool_names = discovered
            return
        raise SkuMappingRuntimeError("sku_mapping_mcp_initialization_failed", runtime=runtime)

    @staticmethod
    def _prompt(task: SkuMappingTask) -> str:
        """Build compact instructions embedding the fixed candidate's own content.

        Unlike `document_analysis`'s prompt (identifiers only, content behind
        tools), this includes the candidate's already-known, already-bounded
        facts directly: there is no tool that would let the agent "discover"
        them, since they are not being searched for, they are fixed.
        """
        attributes = json.dumps(task.raw_attributes, sort_keys=True, default=str) if task.raw_attributes else "{}"
        evidence_summary = "; ".join(
            f"page {reference.page_number} " + (f"block {reference.block_id}" if reference.block_id else f"table {reference.table_id}")
            for reference in task.all_evidence
        ) or "none"
        return (
            "Decide the authoritative catalog SKU, if any, for exactly one fixed product candidate. "
            "Follow the sku-mapping skill. "
            "Always call retrieve_skus first; it scores this fixed candidate, it does not take search text from you. "
            "Before proposing a match, call get_authoritative_sku for the specific SKU you intend to choose. "
            "Use get_evidence_region on the candidate's own evidence when the facts below are not enough to decide confidently; "
            "a low-scoring or empty retrieval is not proof nothing matches. "
            "When finished, call submit_sku_mapping_decision exactly once with the complete decision; it is the only completion mechanism. "
            "Do not return a final answer before that call. Reserve enough time for one correction. If it returns safe validation issues, correct only those issues and submit once more when allowed. "
            "Do not perform money or date arithmetic, normalize other contract-item attributes, or reconsider the commercial status. "
            "Cite evidence_ids as the exact block/table identifiers already listed below (the part after "
            "'block '/'table '), never a full evidence object and never an identifier not listed here. "
            f"candidate_id={task.candidate_id}; "
            f"raw_name={task.raw_name!r}; raw_attributes={attributes}; "
            f"commercial_status={task.commercial_status.value}; commercial_status_rationale={task.commercial_status_rationale!r}; "
            f"evidence=[{evidence_summary}]."
        )

    @staticmethod
    def _record_sdk_event(
        runtime: SkuMappingRuntimeSummary,
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
        if getattr(message, "api_error_status", None) in {401, 403}: return "sku_mapping_authentication_failed"
        return {"max_turns": "sku_mapping_max_turns", "timeout": "sku_mapping_timeout", "budget": "sku_mapping_budget_exceeded"}.get(reason or "", "sku_mapping_runtime_failed")

    def _build_adapters(self, task: SkuMappingTask, tools: SkuMappingToolset):
        """Create three read-only adapters and one in-memory guarded finalizer."""
        runtime = SkuMappingRuntimeSummary(
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
                if name == "retrieve_skus":
                    runtime.catalog_version_id = result.catalog_version_id
                    runtime.last_retrieval_result = result
                elif name == "get_authoritative_sku":
                    runtime.authoritative_skus_by_id[result.id] = result
                elif hasattr(value, "evidence"):
                    reference = value.evidence
                    runtime.referenced_pages = tuple(sorted({*runtime.referenced_pages, reference.page_number}))
                    runtime.referenced_evidence_ids = tuple(sorted({*runtime.referenced_evidence_ids, reference.block_id or reference.table_id}))
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
            except (SkuMappingToolError, ValidationError, ValueError, TypeError) as exc:
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
        @sdk.tool("retrieve_skus", "Score the fixed candidate against the active catalog.", _schema(
            {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_RETRIEVED_SKUS}}, [],
        ))
        async def retrieve(args): return await invoke("retrieve_skus", RetrieveSkusInput, args)
        @sdk.tool("get_authoritative_sku", "Independently confirm one catalog SKU by id or code.", _schema(
            {"sku_id": {"type": "string"}, "sku_code": {"type": "string"}}, [],
        ))
        async def authoritative(args): return await invoke("get_authoritative_sku", GetAuthoritativeSkuInput, args)
        @sdk.tool("get_evidence_region", "Resolve one exact persisted evidence region.", _schema(
            {"evidence": {"type": "object"}}, ["evidence"],
        ))
        async def evidence_region(args): return await invoke(
            "get_evidence_region", EvidenceRegionInput, args,
            {"organization_id": task.organization_id, "preprocessing_run_id": task.preprocessing_run_id},
        )

        @sdk.tool(FINALIZER_TOOL_NAME, "Submit one validated SKU-mapping decision and stop.", self._finalizer_input_schema())
        async def finalize(args):
            sequence_number = runtime.begin_tool_invocation(
                FINALIZER_TOOL_NAME, self._utc_clock(), self._monotonic_clock(),
            )
            runtime.finalization_submission_count += 1
            schema_validated = False
            try:
                if set(args) != {"decision"} or not isinstance(args.get("decision"), dict):
                    raise ValueError("invalid finalization input")
                submitted = args["decision"]
                schema_validated = True
                if TRUSTED_IDENTITY_FIELDS.intersection(submitted):
                    runtime.failure_stage = "trusted_identity_mismatch"
                    runtime.validation_issue_codes = ("trusted_identity_override",)
                    runtime.validation_issues = (
                        {"code": "trusted_identity_override", "location": "decision"},
                    )
                    result, correctable = None, False
                else:
                    resolved_evidence = self._resolve_evidence_ids(task, submitted.get("evidence_ids"), runtime)
                    if resolved_evidence is None:
                        runtime.failure_stage = "evidence_identifier_unresolved"
                        runtime.validation_issue_codes = ("evidence_identifier_unresolved",)
                        if not runtime.validation_issues:
                            runtime.validation_issues = (
                                {"code": "evidence_identifier_unresolved", "location": "decision:evidence_ids"},
                            )
                        result, correctable = None, True
                    else:
                        proposed = {
                            **submitted, "evidence": resolved_evidence,
                            **self._trusted_result_fields(task, runtime),
                        }
                        proposed.pop("evidence_ids", None)
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
                    return {"content": [{"type": "text", "text": "Decision accepted. Stop now."}]}

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
                    and self._settings.sku_mapping_max_corrections > 0
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
                runtime.pydantic_errors = ({"location": "decision", "type": "invalid_input"},)
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
        return [retrieve, authoritative, evidence_region, finalize], runtime


def _schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    """Build a closed JSON Schema for one operation-only Claude tool input."""
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }
