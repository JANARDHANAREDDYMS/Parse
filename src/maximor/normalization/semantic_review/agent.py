"""Guarded Claude semantic-review agent for bounded normalization ambiguity."""

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
from maximor.normalization.semantic_review.contracts import review_item_id
from maximor.normalization.semantic_review.errors import SemanticReviewConfigurationError, SemanticReviewRuntimeError, SemanticReviewValidationError
from maximor.normalization.semantic_review.schemas import NormalizationSemanticReviewResult, NormalizationSemanticReviewTask, SemanticReviewOutcome
from maximor.normalization.semantic_review.tools import NormalizationSemanticReviewTools
from maximor.normalization.semantic_review.versions import SEMANTIC_REVIEW_AGENT_VERSION, SEMANTIC_REVIEW_PROMPT_VERSION, SEMANTIC_REVIEW_SCHEMA_VERSION, SEMANTIC_REVIEW_SKILL_VERSION

MCP_SERVER_NAME = "maximor_normalization_semantic_review"
TOOL_NAMES = ("get_review_item_context", "get_review_item_evidence", "get_candidate_context", "get_term_context", "finalize_normalization_semantic_review")
MCP_TOOL_NAMES = tuple(f"mcp__{MCP_SERVER_NAME}__{name}" for name in TOOL_NAMES)
FINALIZER_TOOL_NAME = TOOL_NAMES[-1]


class NormalizationSemanticReviewAgent(Protocol):
    """Review only assigned deterministic normalization issues."""

    async def review(self, task: NormalizationSemanticReviewTask, tools: NormalizationSemanticReviewTools) -> NormalizationSemanticReviewResult: ...


class UnconfiguredNormalizationSemanticReviewAgent:
    """Explicitly fail until a configured semantic-review agent is selected."""

    async def review(self, task, tools):
        del task, tools
        raise SemanticReviewConfigurationError


@dataclass
class SemanticReviewRuntimeSummary:
    """Bounded operational metadata; never stores prompts, values, or tool bodies."""

    request_id: uuid.UUID = field(default_factory=uuid.uuid4)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    successful_tool_call_order: tuple[str, ...] = ()
    retrieved_evidence_ids: tuple[str, ...] = ()
    tool_call_count: int = 0
    correction_attempt_count: int = 0
    finalizer_submission_count: int = 0
    finalizer_accepted: bool = False
    terminal_reason: str | None = None
    failure_stage: str | None = None
    validation_issue_codes: tuple[str, ...] = ()
    sdk_event_types: tuple[str, ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    session_initialization_succeeded: bool | None = None
    session_initialization_elapsed_ms: int | None = None
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    tool_events_truncated: bool = False
    _tool_sequence: int = field(default=0, repr=False)
    _monotonic_started: float = field(default_factory=time.monotonic, repr=False)

    def record_successful_tool(self, name: str, *, evidence_id: str | None = None) -> None:
        """Record only successful adapter returns."""
        self.tool_call_count += 1
        self._tool_sequence += 1
        self.successful_tool_call_order = (*self.successful_tool_call_order, name)
        if evidence_id:
            self.retrieved_evidence_ids = tuple(sorted({*self.retrieved_evidence_ids, evidence_id}))
        if len(self.tool_events) < 100:
            self.tool_events.append({"tool_name": name[:100], "sequence_number": self._tool_sequence, "succeeded": True})
        else:
            self.tool_events_truncated = True

    def safe_summary(self) -> dict[str, Any]:
        """Return bounded diagnostics safe for callers and tests."""
        return {
            "request_id": str(self.request_id),
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "successful_tool_call_order": list(self.successful_tool_call_order[:50]),
            "retrieved_evidence_ids": list(self.retrieved_evidence_ids[:50]),
            "tool_call_count": self.tool_call_count,
            "correction_attempt_count": self.correction_attempt_count,
            "finalizer_submission_count": self.finalizer_submission_count,
            "finalizer_accepted": self.finalizer_accepted,
            "terminal_reason": self.terminal_reason,
            "failure_stage": self.failure_stage,
            "validation_issue_codes": list(self.validation_issue_codes[:50]),
            "sdk_event_types": list(self.sdk_event_types[:50]),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "session_initialization_succeeded": self.session_initialization_succeeded,
            "session_initialization_elapsed_ms": self.session_initialization_elapsed_ms,
            "tool_events": list(self.tool_events),
            "tool_events_truncated": self.tool_events_truncated,
            "total_elapsed_ms": max(0, int(((time.monotonic() - self._monotonic_started) * 1000))),
        }


@dataclass
class SemanticReviewExecution:
    """Return accepted semantic findings together with bounded runtime facts."""

    result: NormalizationSemanticReviewResult
    runtime: SemanticReviewRuntimeSummary


class ClaudeNormalizationSemanticReviewAgent:
    """Run isolated Claude review with five request-bound tools and one finalizer."""

    def __init__(self, settings: DatabaseSettings, *, client_factory: Callable[[Any], Any] = sdk.ClaudeSDKClient):
        self._settings = settings
        self._client_factory = client_factory

    def build_options(self, task: NormalizationSemanticReviewTask, tools: NormalizationSemanticReviewTools, *, capture: dict[str, Any] | None = None):
        """Build exact namespaced allowlisting with all built-in tools disabled."""
        capture = capture if capture is not None else {}
        key = self._settings.anthropic_api_key
        if key is None or not key.get_secret_value():
            raise SemanticReviewConfigurationError
        adapters = self._build_adapters(task, tools, capture)
        server = sdk.create_sdk_mcp_server(MCP_SERVER_NAME, tools=adapters)
        options = sdk.ClaudeAgentOptions(
            tools=[], allowed_tools=list(MCP_TOOL_NAMES),
            disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch"],
            mcp_servers={MCP_SERVER_NAME: server}, strict_mcp_config=True,
            model=self._settings.document_analysis_model,
            max_turns=self._settings.document_analysis_max_turns,
            max_thinking_tokens=self._settings.document_analysis_max_thinking_tokens,
            max_budget_usd=self._settings.document_analysis_max_budget_usd,
            cwd=self._settings.document_analysis_project_root.resolve(), settings=None,
            setting_sources=["project"], skills=["normalization-semantic-review"], plugins=[],
            env={"ANTHROPIC_API_KEY": key.get_secret_value()},
        )
        object.__setattr__(options, "_maximor_adapters", adapters)
        return options

    async def review(self, task: NormalizationSemanticReviewTask, tools: NormalizationSemanticReviewTools) -> NormalizationSemanticReviewResult:
        """Execute one bounded session and return only a validated finalizer result."""
        return (await self.execute(task, tools)).result

    async def execute(self, task: NormalizationSemanticReviewTask, tools: NormalizationSemanticReviewTools) -> SemanticReviewExecution:
        """Run the client lifecycle with safe cleanup and one correction maximum."""
        runtime = SemanticReviewRuntimeSummary()
        capture: dict[str, Any] = {"result": None, "rejected": False}
        bound_tools = NormalizationSemanticReviewTools(task, getattr(tools, "_resolver", None), runtime)
        options = self.build_options(task, bound_tools, capture=capture)
        client = None
        try:
            initialization_started = time.monotonic()
            async with asyncio.timeout(self._settings.document_analysis_timeout_seconds):
                client = self._client_factory(options)
                await client.connect()
                runtime.session_initialization_elapsed_ms = max(0, int((time.monotonic() - initialization_started) * 1000))
                runtime.session_initialization_succeeded = True
                await client.query(self._prompt(task))
                async for message in client.receive_response():
                    runtime.sdk_event_types = tuple(sorted({*runtime.sdk_event_types, message.__class__.__name__}))
                    if isinstance(message, sdk.ResultMessage):
                        runtime.terminal_reason = getattr(message, "terminal_reason", None) or getattr(message, "stop_reason", None)
                        usage = getattr(message, "usage", None) or {}
                        runtime.input_tokens = usage.get("input_tokens")
                        runtime.output_tokens = usage.get("output_tokens")
                        runtime.cost_usd = getattr(message, "total_cost_usd", None)
                    if capture["result"] is not None:
                        break
            if capture["result"] is None:
                runtime.failure_stage = "finalizer_not_called"
                raise SemanticReviewRuntimeError("normalization_semantic_review_finalizer_not_called", runtime=runtime)
            runtime.finalizer_accepted = True
            runtime.terminal_reason = "accepted_by_finalizer"
            return SemanticReviewExecution(result=capture["result"], runtime=runtime)
        except asyncio.TimeoutError:
            runtime.failure_stage = "timeout"
            raise SemanticReviewRuntimeError("normalization_semantic_review_timeout", runtime=runtime) from None
        except (SemanticReviewRuntimeError, SemanticReviewValidationError):
            raise
        except Exception:
            runtime.failure_stage = runtime.failure_stage or "sdk_transport"
            raise SemanticReviewRuntimeError("normalization_semantic_review_runtime_failed", runtime=runtime) from None
        finally:
            runtime.completed_at = datetime.now(UTC)
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    def _build_adapters(self, task, tools, capture):
        """Build SDK adapters whose scope is fixed by the trusted task."""
        async def context(args):
            return {"content": [{"type": "text", "text": json.dumps((await tools.get_review_item_context(args.get("review_item_id"))).model_dump(mode="json"), sort_keys=True)}]}
        async def evidence(args):
            value = await tools.get_review_item_evidence(args.get("review_item_id"), args.get("evidence_id"))
            return {"content": [{"type": "text", "text": "Evidence resolved." if value is not None else "Evidence unavailable."}]}
        async def candidate(args):
            value = await tools.get_candidate_context(args.get("candidate_id"))
            return {"content": [{"type": "text", "text": json.dumps([item.model_dump(mode="json") for item in value], sort_keys=True)}]}
        async def term(args):
            value = await tools.get_term_context(args.get("term_id"))
            return {"content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}]}
        async def finalize(args):
            runtime = tools._runtime
            try:
                result = self.validate_submission(task, args.get("result") if isinstance(args, dict) else None, runtime)
                capture["result"] = result
                runtime.finalizer_accepted = True
                return {"content": [{"type": "text", "text": "Accepted. Stop now."}]}
            except SemanticReviewValidationError:
                if runtime.correction_attempt_count < 1:
                    runtime.correction_attempt_count += 1
                    return {"content": [{"type": "text", "text": json.dumps({"correction_allowed": True, "issue_codes": list(runtime.validation_issue_codes)}, sort_keys=True)}], "is_error": True}
                capture["rejected"] = True
                return {"content": [{"type": "text", "text": json.dumps({"correction_allowed": False, "issue_codes": list(runtime.validation_issue_codes)}, sort_keys=True)}], "is_error": True}
        return [
            sdk.tool("get_review_item_context", "Read one assigned review item.", {"type": "object", "properties": {"review_item_id": {"type": "string"}}, "required": ["review_item_id"]})(context),
            sdk.tool("get_review_item_evidence", "Resolve assigned evidence by ID.", {"type": "object", "properties": {"review_item_id": {"type": "string"}, "evidence_id": {"type": "string"}}, "required": ["review_item_id", "evidence_id"]})(evidence),
            sdk.tool("get_candidate_context", "Read an assigned candidate context.", {"type": "object", "properties": {"candidate_id": {"type": "string"}}, "required": ["candidate_id"]})(candidate),
            sdk.tool("get_term_context", "Read an assigned term context.", {"type": "object", "properties": {"term_id": {"type": "string"}}, "required": ["term_id"]})(term),
            sdk.tool(FINALIZER_TOOL_NAME, "Submit the complete semantic review.", {"type": "object", "properties": {"result": {"type": "object"}}, "required": ["result"]})(finalize),
        ]

    @staticmethod
    def validate_submission(task: NormalizationSemanticReviewTask, submitted: Any, runtime: SemanticReviewRuntimeSummary) -> NormalizationSemanticReviewResult:
        """Validate trusted identity, exact coverage, and same-session evidence IDs."""
        runtime.finalizer_submission_count += 1
        try:
            payload = dict(submitted)
            payload.update({
                "schema_version": SEMANTIC_REVIEW_SCHEMA_VERSION,
                "organization_id": task.request.organization_id,
                "document_id": task.request.document_id,
                "preprocessing_run_id": task.request.preprocessing_run_id,
                "analysis_run_id": task.request.analysis_run_id,
                "normalization_schema_version": task.request.normalization_schema_version,
                "finalization_policy_version": task.request.finalization_policy_version,
                "prompt_version": task.request.prompt_version,
                "skill_version": task.request.skill_version,
                "agent_version": task.request.agent_version,
            })
            result = NormalizationSemanticReviewResult.model_validate(payload)
        except (ValidationError, TypeError, ValueError):
            runtime.failure_stage = "pydantic_schema_validation"
            runtime.validation_issue_codes = ("semantic_review_schema_invalid",)
            raise SemanticReviewValidationError("normalization_semantic_review_invalid_output", runtime=runtime) from None
        expected = set(task.request.review_item_ids)
        actual = {finding.review_item_id for finding in result.findings}
        if actual != expected or tuple(f.review_item_id for f in result.findings) != task.request.review_item_ids:
            runtime.failure_stage = "completion_gate_failure"
            runtime.validation_issue_codes = ("review_item_coverage_invalid",)
            raise SemanticReviewValidationError("normalization_semantic_review_invalid_output", runtime=runtime)
        items = {item.review_item_id: item for item in task.items}
        for finding in result.findings:
            item = items[finding.review_item_id]
            if finding.candidate_id is not None and finding.candidate_id != item.candidate_id:
                runtime.failure_stage = "trusted_identity_mismatch"
                runtime.validation_issue_codes = ("candidate_reference_invalid",)
                raise SemanticReviewValidationError("normalization_semantic_review_invalid_output", runtime=runtime)
            if finding.outcome in {SemanticReviewOutcome.SUPPORTED_INTERPRETATION, SemanticReviewOutcome.ROUTE_FOR_TARGETED_CORRECTION}:
                if not finding.evidence_ids or not set(finding.evidence_ids).issubset(set(item.allowed_evidence_ids) & set(runtime.retrieved_evidence_ids)):
                    runtime.failure_stage = "evidence_validation_failure"
                    runtime.validation_issue_codes = ("evidence_not_retrieved",)
                    raise SemanticReviewValidationError("normalization_semantic_review_invalid_output", runtime=runtime)
        return result

    @staticmethod
    def _prompt(task: NormalizationSemanticReviewTask) -> str:
        """Create a compact prompt containing no source document content."""
        return "Review only the assigned normalization review items. Use get_review_item_context first and get_review_item_evidence before any affirmative interpretation. Prefer insufficient_evidence or conflict_unresolved over guessing. Never calculate money, change a SKU, create candidates, or apply corrections. Submit exactly one finding per assigned item through finalize_normalization_semantic_review, then stop. Assigned item IDs: " + ",".join(task.request.review_item_ids)
