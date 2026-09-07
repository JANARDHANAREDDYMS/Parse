"""Exercise the isolated Claude SDK client transport and document adapters locally."""

import asyncio
import inspect
import json
import uuid
from datetime import UTC, datetime

import claude_agent_sdk as sdk
import pytest

from maximor.config import DatabaseSettings
from maximor.document_analysis.agent import (
    MCP_SERVER_NAME,
    MCP_TOOL_NAMES,
    MAX_RUNTIME_TIMING_EVENTS,
    FINALIZER_TOOL_NAME,
    READ_ONLY_TOOL_NAMES,
    TOOL_NAMES,
    ClaudeDocumentAnalysisAgent,
    DocumentAnalysisRuntimeSummary,
)
from maximor.document_analysis.contracts import DocumentAnalysisRequest
from maximor.document_analysis.errors import (
    DocumentAnalysisConfigurationError,
    DocumentAnalysisRuntimeError,
    DocumentAnalysisValidationError,
)
from maximor.worker.handlers.document_analysis import DocumentAnalysisHandler


def request() -> DocumentAnalysisRequest:
    """Return generated identifier-only input."""

    return DocumentAnalysisRequest(
        organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=uuid.uuid4(), preprocessing_schema_version="1",
        document_analysis_schema_version="1", prompt_version="p1",
        skill_version="skill-1", agent_version="a1",
    )


def settings(key: str | None = "secret-test-key", **overrides) -> DatabaseSettings:
    """Return test-only centralized configuration."""

    values = dict(
        database_url="postgresql+asyncpg://test", anthropic_api_key=key,
        document_analysis_project_root="..",
    )
    values.update(overrides)
    return DatabaseSettings(**values)


class Tools:
    """Generated tool service recording only scope-bound inputs."""

    def __init__(self) -> None:
        self.calls = []

    async def get_document_overview(self, value): self.calls.append(value); return {"ok": True}
    async def search_document(self, value): self.calls.append(value); return ()
    async def get_page_text(self, value): self.calls.append(value); return ()
    async def get_page_blocks(self, value): self.calls.append(value); return ()
    async def get_page_tables(self, value): self.calls.append(value); return ()
    async def get_page_render(self, value):
        self.calls.append(value)
        return type("Render", (), {
            "content": b"x", "media_type": "image/png", "page_number": value.page_number,
            "sha256_checksum": "0" * 64, "pixel_width": 1, "pixel_height": 1, "dpi": 72,
        })()
    async def get_evidence_region(self, value): self.calls.append(value); return {"ok": True}


def valid_output(value: DocumentAnalysisRequest) -> dict[str, str]:
    """Return the smallest schema-valid output linked to the request."""

    evidence = {
        "preprocessing_run_id": str(value.preprocessing_run_id), "page_number": 1,
        "block_id": "native:p0001:b000000", "representation": "native_text",
        "extraction_source": "native",
    }
    return {
        "schema_version": value.document_analysis_schema_version,
        "organization_id": str(value.organization_id), "document_id": str(value.document_id),
        "preprocessing_run_id": str(value.preprocessing_run_id),
        "preprocessing_schema_version": value.preprocessing_schema_version,
        "prompt_version": value.prompt_version, "agent_version": value.agent_version,
        "product_candidates": [{"candidate_id": "candidate-0001", "raw_name": "Generated", "evidence": [evidence]}],
        "evidence_references": [evidence],
    }


def finalization_submission(value: DocumentAnalysisRequest, payload=None) -> dict:
    """Remove application-bound fields from one semantic finalizer submission."""

    submitted = dict(payload or valid_output(value))
    for field_name in (
        "schema_version", "organization_id", "document_id", "preprocessing_run_id",
        "preprocessing_schema_version", "prompt_version", "agent_version",
    ):
        submitted.pop(field_name, None)
    return submitted


def submit_finalization(value: DocumentAnalysisRequest, payload=None):
    """Return an async fake callback that grounds then submits one semantic result."""

    async def callback(options):
        ground_runtime(options)
        return await options._maximor_adapters[-1].handler(
            {"result": finalization_submission(value, payload)},
        )

    return callback


def ground_runtime(options) -> None:
    """Simulate two successful request-bound tool adapters without result bodies."""

    runtime = getattr(options, "_maximor_runtime")
    if runtime.tool_call_count == 0:
        runtime.tool_call_count = 2
        runtime.tool_calls_by_name = {"get_document_overview": 1, "get_page_text": 1}
        runtime.successful_tool_call_order = ("get_document_overview", "get_page_text")
        runtime.referenced_pages = (1,)


class FakeClient:
    """Simulate the installed stateful client without subprocess or network activity."""

    def __init__(self, options=None, *, messages=(), responses=None, connect_error=None, status_error=None, query_error=None, tools=TOOL_NAMES, mcp_servers=None, on_query=None, response_delay_seconds=0.0):
        self.options, self.messages = options, list(messages)
        self.responses = [list(items) for items in responses] if responses is not None else None
        self.response_index = self.query_count = 0
        self.connect_error, self.status_error, self.query_error, self.tools, self.mcp_servers, self.on_query = connect_error, status_error, query_error, tools, mcp_servers, on_query
        self.response_delay_seconds = response_delay_seconds
        self.connected = self.queried = self.disconnected = self.interrupted = False

    async def connect(self):
        self.connected = True
        if self.connect_error: raise self.connect_error

    async def get_mcp_status(self):
        if self.status_error: raise self.status_error
        if self.mcp_servers is not None: return {"mcpServers": self.mcp_servers}
        return {"mcpServers": [{"name": MCP_SERVER_NAME, "status": "connected", "tools": [{"name": name} for name in self.tools]}]}

    async def query(self, prompt):
        self.queried = True
        self.query_count += 1
        if self.query_count == 1: assert "get_document_overview first" in prompt
        if self.on_query:
            value = self.on_query(self.options)
            if inspect.isawaitable(value):
                await value
        if self.query_error: raise self.query_error

    async def receive_response(self):
        if self.response_delay_seconds:
            await asyncio.sleep(self.response_delay_seconds)
        messages = self.messages
        if self.responses is not None:
            messages = self.responses[self.response_index]
            self.response_index += 1
        for message in messages: yield message

    async def disconnect(self): self.disconnected = True

    async def interrupt(self): self.interrupted = True


def factory_for(client: FakeClient):
    """Inject one test client and retain the production-built options."""

    def factory(options):
        client.options = options
        return client
    return factory


@pytest.mark.asyncio
async def test_missing_key_fails_without_disclosure():
    """Fail before constructing an SDK client when configuration is absent."""

    with pytest.raises(DocumentAnalysisConfigurationError) as caught:
        await ClaudeDocumentAnalysisAgent(settings(None)).analyze(request(), Tools())
    assert "secret" not in str(caught.value).lower()


def test_options_have_exact_namespaced_tools_and_project_skill():
    """Inspect locally created isolated SDK options and the in-process server."""

    options = ClaudeDocumentAnalysisAgent(settings()).build_options(request(), Tools())
    assert tuple(options.allowed_tools) == MCP_TOOL_NAMES
    assert options.tools == [] and options.strict_mcp_config
    assert options.skills == ["order-form-analysis"] and options.setting_sources == ["project"]
    assert set(options.mcp_servers) == {MCP_SERVER_NAME}
    assert options.mcp_servers[MCP_SERVER_NAME]["type"] == "sdk"
    assert options.plugins == [] and "Bash" in options.disallowed_tools
    assert options.output_format is None
    assert options.max_thinking_tokens == 4096
    assert "secret-test-key" not in repr(getattr(options, "_maximor_runtime"))


def test_finalizer_schema_exposes_only_semantic_result_fields():
    """Keep identity/version fields application-bound while retaining Pydantic definitions."""

    schema = ClaudeDocumentAnalysisAgent._finalizer_input_schema()
    result = schema["properties"]["result"]
    assert "$defs" in schema
    assert "organization_id" not in result["properties"]
    assert "preprocessing_run_id" not in result["properties"]
    assert "schema_version" not in result["properties"]


@pytest.mark.parametrize(("message", "expected"), [
    ("identifiers must be canonical", "identifiers_not_canonical"),
    ("analysis result identifiers must be unique and ordered", "duplicate_identifier"),
    ("commercial status references an unknown product candidate", "unknown_status_candidate"),
    ("evidence references must belong to the analysis preprocessing run", "evidence_run_mismatch"),
    ("product candidates must not contain SKU mapping values", "candidate_contains_sku_mapping"),
    ("product candidate has too many raw attributes", "candidate_attribute_bounds_exceeded"),
    ("evidence requires exactly one block_id or table_id", "evidence_target_invalid"),
])
def test_known_root_validation_messages_map_to_safe_codes(message, expected):
    """Expose stable root-validator codes without retaining validator values."""

    assert ClaudeDocumentAnalysisAgent._map_pydantic_error({"type": "value_error", "msg": message}) == expected


def test_structured_order_is_normalized_before_validation():
    """Sort stable-ID collections while leaving duplicate detection to validation."""

    value = request()
    runtime = DocumentAnalysisRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC))
    runtime.tool_calls_by_name = {"get_document_overview": 1, "get_page_text": 1}
    raw = finalization_submission(value)
    candidate_evidence = raw["product_candidates"][0]["evidence"]
    raw["product_candidates"] = [
        {"candidate_id": "candidate-0002", "raw_name": "Second", "evidence": candidate_evidence},
        {"candidate_id": "candidate-0001", "raw_name": "First", "evidence": candidate_evidence},
    ]
    result, issues = ClaudeDocumentAnalysisAgent(settings())._validate_structured_output(
        value, {**raw, "organization_id": str(value.organization_id), "document_id": str(value.document_id), "preprocessing_run_id": str(value.preprocessing_run_id), "schema_version": value.document_analysis_schema_version, "preprocessing_schema_version": value.preprocessing_schema_version, "prompt_version": value.prompt_version, "agent_version": value.agent_version}, runtime,
    )
    assert result is not None and issues is False
    assert [item.candidate_id for item in result.product_candidates] == ["candidate-0001", "candidate-0002"]
    duplicate = dict(raw)
    duplicate["product_candidates"] = [
        {"candidate_id": "candidate-0001", "raw_name": "First", "evidence": candidate_evidence},
        {"candidate_id": "candidate-0001", "raw_name": "Duplicate", "evidence": candidate_evidence},
    ]
    result, _ = ClaudeDocumentAnalysisAgent(settings())._validate_structured_output(
        value, {**duplicate, "organization_id": str(value.organization_id), "document_id": str(value.document_id), "preprocessing_run_id": str(value.preprocessing_run_id), "schema_version": value.document_analysis_schema_version, "preprocessing_schema_version": value.preprocessing_schema_version, "prompt_version": value.prompt_version, "agent_version": value.agent_version}, runtime,
    )
    assert result is None and runtime.pydantic_errors


@pytest.mark.asyncio
async def test_configured_in_process_server_lists_exactly_eight_tools_before_connect():
    """Discover all seven readers plus finalizer without a client session."""

    agent = ClaudeDocumentAnalysisAgent(settings())
    options = agent.build_options(request(), Tools())
    assert await agent._configured_mcp_tool_names(options) == tuple(sorted(TOOL_NAMES))
    assert len(TOOL_NAMES) == 8 and FINALIZER_TOOL_NAME in TOOL_NAMES


def test_prompt_is_identifier_only_and_requires_grounded_retrieval():
    """Keep source content, paths, SQL, and credentials out of the prompt."""

    value = request()
    prompt = ClaudeDocumentAnalysisAgent._prompt(value)
    assert str(value.organization_id) in prompt and str(value.preprocessing_run_id) in prompt
    assert "get_document_overview first" in prompt and "content-retrieval tool" in prompt
    assert "storage_key" not in prompt and "SELECT " not in prompt and "secret-test-key" not in prompt


@pytest.mark.asyncio
async def test_eight_adapters_are_directly_invocable_and_scope_bound():
    """Call seven readers and the in-memory finalizer without a client connection."""

    value, tools = request(), Tools()
    adapters, runtime = ClaudeDocumentAnalysisAgent(settings())._build_adapters(value, tools)
    arguments = [
        {}, {"query": "term", "limit": 1},
        {"page_number": 1, "representation": "native", "limit": 1},
        {"page_number": 1, "representation": "layout", "limit": 1},
        {"page_number": 1, "limit": 1}, {"page_number": 1},
        {"evidence": {"preprocessing_run_id": str(value.preprocessing_run_id), "page_number": 1, "block_id": "layout:p0001:b000000", "representation": "layout"}},
    ]
    for tool_name, adapter, tool_arguments in zip(READ_ONLY_TOOL_NAMES, adapters[:-1], arguments, strict=True):
        response = await adapter.handler(tool_arguments)
        assert not response.get("is_error"), tool_name
    assert len(tools.calls) == len(READ_ONLY_TOOL_NAMES) == runtime.tool_call_count
    assert len(runtime.adapter_invocations) == len(READ_ONLY_TOOL_NAMES)
    assert all(item["schema_validation_reached"] and item["adapter_succeeded"] for item in runtime.adapter_invocations)
    assert all(call.organization_id == value.organization_id and call.preprocessing_run_id == value.preprocessing_run_id for call in tools.calls)
    rejected = await adapters[2].handler({"page_number": 1, "representation": "native", "organization_id": str(uuid.uuid4())})
    assert rejected["is_error"] and runtime.failed_tool_call_count == 1
    assert runtime.adapter_invocations[-1]["schema_validation_reached"] is False
    assert runtime.adapter_invocations[-1]["failure_category"] == "schema_rejected"
    starts = [event for event in runtime.timing_events if event["kind"] == "tool_invocation_started"]
    completions = [event for event in runtime.timing_events if event["kind"] == "tool_invocation_completed"]
    assert [event["sequence_number"] for event in starts] == list(range(1, 9))
    assert [event["succeeded"] for event in completions] == [True] * 7 + [False]
    assert runtime.last_successful_tool_return_at is not None
    runtime.finalize_timing(
        datetime.now(UTC), runtime.monotonic_started_at + 0.1,
    )
    assert runtime.elapsed_after_last_successful_tool_return_ms is not None
    assert runtime.elapsed_after_last_successful_tool_return_ms >= 0
    final_response = await adapters[-1].handler({"result": finalization_submission(value)})
    assert not final_response.get("is_error")
    assert runtime.finalization_accepted


@pytest.mark.asyncio
async def test_finalizer_rejects_ungrounded_and_schema_invalid_submissions_safely():
    """Return only safe codes and locations, never rejected semantic values."""

    value = request()
    adapters, runtime = ClaudeDocumentAnalysisAgent(settings())._build_adapters(value, Tools())
    invalid = finalization_submission(value)
    invalid["product_candidates"][0]["raw_name"] = "x" * 2_001
    response = await adapters[-1].handler({"result": invalid})
    assert response["is_error"] and runtime.failure_stage == "pydantic_schema_validation"
    assert "x" * 100 not in repr(response)
    assert runtime.correction_attempt_count == 1

    ungrounded = await ClaudeDocumentAnalysisAgent(settings())._build_adapters(value, Tools())[0][-1].handler(
        {"result": finalization_submission(value)},
    )
    assert ungrounded["is_error"]


@pytest.mark.asyncio
async def test_finalizer_names_the_specific_malformed_top_level_shape_and_allows_correction():
    """A malformed top-level submission is a specific, correctable mistake, not a bare dead end.

    Two real production failures (`document_analysis_invalid_output`, no
    further detail) traced back to this exact path: the submission's
    top-level shape didn't match `{"result": <object>}` at all. Before this
    fix, that always produced a bare `invalid_input` code with
    `correction_allowed: False` -- the model got no specific signal and no
    chance to fix it. It must now name which shape was wrong and get the
    same one-shot correction every other rejection gets.
    """

    value = request()
    adapters, runtime = ClaudeDocumentAnalysisAgent(settings())._build_adapters(value, Tools())
    response = await adapters[-1].handler({"unexpected_key": {}})
    assert response["is_error"]
    issues = json.loads(response["content"][0]["text"])["issues"]
    assert issues[0]["code"].startswith("unexpected_top_level_keys:")
    assert json.loads(response["content"][0]["text"])["correction_allowed"] is True
    assert runtime.correction_attempt_count == 1

    adapters, runtime = ClaudeDocumentAnalysisAgent(settings())._build_adapters(value, Tools())
    response = await adapters[-1].handler({"result": "not an object"})
    issues = json.loads(response["content"][0]["text"])["issues"]
    assert issues[0]["code"] == "not_an_object:str"


def test_finalizer_keeps_persistence_handler_owned():
    """Keep the submission boundary in-memory; only the worker handler persists output."""

    agent_source = inspect.getsource(ClaudeDocumentAnalysisAgent)
    handler_source = inspect.getsource(DocumentAnalysisHandler.execute)
    assert "save_completed_result(" not in agent_source
    assert "save_completed_result(" in handler_source


@pytest.mark.asyncio
async def test_client_lifecycle_confirms_mcp_discovery_and_streams_output():
    """Verify connect, MCP status, prompt, stream, and disconnect through a fake client."""

    value = request()
    assistant = sdk.AssistantMessage(content=[], model="test")
    terminal = sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="s", terminal_reason="completed")
    client = FakeClient(messages=[assistant, terminal], on_query=submit_finalization(value))
    execution = await ClaudeDocumentAnalysisAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert client.connected and client.queried and client.disconnected
    assert execution.runtime.mcp_server_status == "connected"
    assert execution.runtime.announced_tool_names == tuple(sorted(TOOL_NAMES))
    assert execution.result.preprocessing_run_id == value.preprocessing_run_id
    assert execution.runtime.session_initialization_succeeded is True
    assert execution.runtime.session_initialization_elapsed_ms is not None
    assert execution.runtime.terminal_kind == "accepted_by_finalizer"
    assert execution.runtime.total_elapsed_ms is not None
    assert execution.runtime.total_elapsed_ms >= 0
    sdk_events = [event for event in execution.runtime.timing_events if event["kind"] == "sdk_event_received"]
    assert {event["event_type"] for event in sdk_events} == {"AssistantMessage", "ResultMessage"}
    assert all(event["timestamp"].endswith("+00:00") for event in sdk_events)


@pytest.mark.asyncio
async def test_empty_sdk_status_is_not_reported_but_does_not_block_execution():
    """Accept SDK omission of an idle in-process server after local registration passed."""

    value = request()
    terminal = sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s", terminal_reason="completed")
    client = FakeClient(messages=[terminal], mcp_servers=[], on_query=submit_finalization(value))
    execution = await ClaudeDocumentAnalysisAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert execution.runtime.mcp_server_status == "not_reported" and client.disconnected


@pytest.mark.asyncio
async def test_accepted_finalization_without_result_interrupts_after_grace(monkeypatch):
    """Accept the in-memory result and close an SDK stream lacking a ResultMessage."""

    import maximor.document_analysis.agent as agent_module

    monkeypatch.setattr(agent_module, "FINALIZER_RESULT_GRACE_SECONDS", 0.01)
    value = request()
    client = FakeClient(
        messages=[], on_query=submit_finalization(value), response_delay_seconds=0.05,
    )
    execution = await ClaudeDocumentAnalysisAgent(
        settings(), client_factory=factory_for(client),
    ).execute(value, Tools())
    assert execution.result.preprocessing_run_id == value.preprocessing_run_id
    assert execution.runtime.terminal_reason == "accepted_by_finalizer"
    assert execution.runtime.input_tokens is None and execution.runtime.cost_usd is None
    assert client.interrupted and client.disconnected


@pytest.mark.asyncio
async def test_explicit_failed_sdk_mcp_status_remains_fatal():
    """Reject an SDK-reported failed server even though local registration exists."""

    client = FakeClient(mcp_servers=[{"name": MCP_SERVER_NAME, "status": "failed"}])
    with pytest.raises(DocumentAnalysisRuntimeError) as caught:
        await ClaudeDocumentAnalysisAgent(settings(), client_factory=factory_for(client)).execute(request(), Tools())
    assert caught.value.code == "document_analysis_mcp_initialization_failed" and client.disconnected


@pytest.mark.asyncio
async def test_incorrect_local_registration_is_fatal_before_client_connect():
    """Fail closed if the configured in-process server does not expose all tools."""

    class BadLocalAgent(ClaudeDocumentAnalysisAgent):
        def build_options(self, value, tools):
            options = super().build_options(value, tools)
            options.mcp_servers[MCP_SERVER_NAME] = sdk.create_sdk_mcp_server(MCP_SERVER_NAME, tools=[])
            return options

    client = FakeClient()
    with pytest.raises(DocumentAnalysisRuntimeError) as caught:
        await BadLocalAgent(settings(), client_factory=factory_for(client)).execute(request(), Tools())
    assert caught.value.code == "document_analysis_mcp_tool_discovery_failed"
    assert not client.connected and client.disconnected


def test_tool_use_event_names_are_retained_without_arguments_or_content():
    """Record only installed SDK event type names for safe failure diagnostics."""

    runtime = DocumentAnalysisRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC))
    event = sdk.AssistantMessage(content=[sdk.ToolUseBlock(id="tool-id", name="mcp__maximor_document__get_document_overview", input={})], model="test")
    ClaudeDocumentAnalysisAgent._record_sdk_event(runtime, event)
    assert set(runtime.tool_event_types) == {"AssistantMessage", "ToolUseBlock"}
    assert runtime.sdk_tool_use_events[0]["tool_name"] == "mcp__maximor_document__get_document_overview"
    assert runtime.sdk_tool_use_events[0]["allowed"] is True
    assert runtime.sdk_tool_use_events[0]["tool_kind"] == "retrieval"
    assert "tool-id" not in repr(runtime)


@pytest.mark.parametrize(("name", "allowed", "kind"), [
    (f"mcp__{MCP_SERVER_NAME}__{FINALIZER_TOOL_NAME}", True, "finalizer"),
    ("unavailable_tool", False, "unavailable"),
])
def test_tool_use_event_classification_is_value_free(name, allowed, kind):
    """Classify finalizer and unavailable requests without retaining SDK payloads."""

    runtime = DocumentAnalysisRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC))
    event = sdk.AssistantMessage(content=[sdk.ToolUseBlock(id="secret-tool-id", name=name, input={"secret": "value"})], model="test")
    ClaudeDocumentAnalysisAgent._record_sdk_event(runtime, event)
    record = runtime.sdk_tool_use_events[0]
    assert (record["allowed"], record["tool_kind"]) == (allowed, kind)
    assert "secret-tool-id" not in repr(runtime.persistence_diagnostics())
    assert "value" not in repr(runtime.persistence_diagnostics())


@pytest.mark.asyncio
async def test_finalizer_requested_but_adapter_not_reached_is_distinguishable():
    """An SDK event alone does not create an adapter invocation record."""

    runtime = DocumentAnalysisRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC))
    ClaudeDocumentAnalysisAgent._record_sdk_event(
        runtime,
        sdk.AssistantMessage(content=[sdk.ToolUseBlock(id="id", name=f"mcp__{MCP_SERVER_NAME}__{FINALIZER_TOOL_NAME}", input={})], model="test"),
    )
    assert runtime.sdk_tool_use_events[0]["tool_kind"] == "finalizer"
    assert runtime.adapter_invocations == []


@pytest.mark.asyncio
async def test_finalizer_schema_rejection_records_adapter_boundary():
    """A reached finalizer with invalid input records safe schema rejection metadata."""

    value = request()
    adapters, runtime = ClaudeDocumentAnalysisAgent(settings())._build_adapters(value, Tools())
    response = await adapters[-1].handler({"result": {"product_candidates": [{"raw_name": "x" * 2001}]}})
    assert response["is_error"]
    record = runtime.adapter_invocations[-1]
    assert record["tool_name"] == FINALIZER_TOOL_NAME
    assert record["schema_validation_reached"] is True
    assert record["adapter_succeeded"] is False
    assert record["failure_category"] == "schema_rejected"


def test_timing_event_history_is_bounded_and_value_free():
    """Truncate metadata-only timing history rather than retaining an unbounded trace."""

    now = datetime.now(UTC)
    runtime = DocumentAnalysisRuntimeSummary(
        request_id=uuid.uuid4(), started_at=now, monotonic_started_at=0.0,
    )
    for index in range(MAX_RUNTIME_TIMING_EVENTS + 5):
        runtime.record_timing_event(
            kind="sdk_event_received", timestamp=now, monotonic_now=float(index),
            event_type="AssistantMessage",
        )
    diagnostics = runtime.persistence_diagnostics()
    assert len(runtime.timing_events) == MAX_RUNTIME_TIMING_EVENTS
    assert runtime.timing_events_truncated and diagnostics["timing_events_truncated"]
    assert all(event["elapsed_ms"] >= 0 for event in runtime.timing_events)
    assert "prompt" not in repr(diagnostics).lower()


@pytest.mark.asyncio
async def test_timeout_preserves_partial_timing_after_successful_retrieval():
    """Retain successful retrieval facts and timing when no terminal SDK result arrives."""

    value = request()
    client = FakeClient(
        messages=[], on_query=ground_runtime, response_delay_seconds=0.05,
    )
    with pytest.raises(DocumentAnalysisRuntimeError) as caught:
        await ClaudeDocumentAnalysisAgent(
            settings(document_analysis_timeout_seconds=0.01),
            client_factory=factory_for(client),
        ).execute(value, Tools())
    runtime = caught.value.runtime
    assert caught.value.code == "document_analysis_timeout"
    assert runtime.successful_tool_call_order == ("get_document_overview", "get_page_text")
    assert runtime.terminal_kind == "timeout"
    assert runtime.total_elapsed_ms is not None and runtime.total_elapsed_ms >= 0
    assert runtime.session_initialization_succeeded is True
    assert runtime.persistence_diagnostics()["timing_events"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "expected"), [
    ("connect", "document_analysis_sdk_initialization_failed"),
    ("status", "document_analysis_mcp_initialization_failed"),
    ("query", "document_analysis_sdk_transport_failed"),
])
async def test_lifecycle_failures_are_distinct_safe_codes_with_runtime(kind, expected):
    """Preserve safe partial diagnostics while mapping lifecycle failures precisely."""

    client = FakeClient(**{f"{kind}_error": RuntimeError("/private/tmp/secret-test-key")})
    with pytest.raises(DocumentAnalysisRuntimeError) as caught:
        await ClaudeDocumentAnalysisAgent(settings(), client_factory=factory_for(client)).execute(request(), Tools())
    assert caught.value.code == expected and caught.value.runtime is not None and client.disconnected
    assert "/private/tmp" not in str(caught.value) and "secret-test-key" not in repr(caught.value.runtime)


@pytest.mark.asyncio
async def test_discovery_and_missing_finalizer_failures_are_distinct_and_safe():
    """Reject missing tools and a terminal response without finalization."""

    client = FakeClient(tools=TOOL_NAMES[:-1])
    with pytest.raises(DocumentAnalysisRuntimeError) as caught:
        await ClaudeDocumentAnalysisAgent(settings(), client_factory=factory_for(client)).execute(request(), Tools())
    assert caught.value.code == "document_analysis_mcp_tool_discovery_failed" and client.disconnected

    missing = sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s")
    client = FakeClient(messages=[missing], on_query=ground_runtime)
    with pytest.raises(DocumentAnalysisRuntimeError) as caught:
        await ClaudeDocumentAnalysisAgent(settings(), client_factory=factory_for(client)).execute(request(), Tools())
    assert caught.value.code == "document_analysis_finalizer_not_called"
    assert caught.value.runtime is not None and client.disconnected


@pytest.mark.asyncio
async def test_failed_tool_trace_maps_to_distinct_safe_terminal_code():
    """Retain a failed-tool counter and distinguish it from malformed output."""

    def fail_tool(options):
        runtime = getattr(options, "_maximor_runtime")
        runtime.failed_tool_call_count = 1
        runtime.failed_tool_calls_by_name["get_page_text"] = 1

    missing = sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s")
    client = FakeClient(messages=[missing], on_query=fail_tool)
    with pytest.raises(DocumentAnalysisRuntimeError) as caught:
        await ClaudeDocumentAnalysisAgent(settings(), client_factory=factory_for(client)).execute(request(), Tools())
    assert caught.value.code == "document_analysis_finalizer_not_called"
    assert caught.value.runtime.failed_tool_calls_by_name == {"get_page_text": 1}


@pytest.mark.asyncio
async def test_one_correctable_schema_failure_then_success_uses_same_session():
    """Send one value-free correction and accept only the corrected valid result."""

    value = request()
    invalid = valid_output(value)
    invalid["product_candidates"][0]["raw_name"] = "x" * 2_001
    async def submit_then_correct(options):
        ground_runtime(options)
        finalizer = options._maximor_adapters[-1]
        first = await finalizer.handler({"result": finalization_submission(value, invalid)})
        second = await finalizer.handler({"result": finalization_submission(value)})
        assert first["is_error"] and not second.get("is_error")
    client = FakeClient(messages=[], on_query=submit_then_correct)
    execution = await ClaudeDocumentAnalysisAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert client.query_count == 1 and execution.runtime.correction_attempt_count == 1
    assert execution.runtime.failure_stage is None and execution.result.product_candidates


@pytest.mark.asyncio
async def test_second_invalid_output_stops_without_third_request():
    """Bound schema correction to one attempt across the same client session."""

    value = request(); invalid = valid_output(value)
    invalid["product_candidates"][0]["raw_name"] = "x" * 2_001
    async def submit_twice(options):
        ground_runtime(options)
        finalizer = options._maximor_adapters[-1]
        await finalizer.handler({"result": finalization_submission(value, invalid)})
        await finalizer.handler({"result": finalization_submission(value, invalid)})
    client = FakeClient(messages=[], on_query=submit_twice)
    with pytest.raises(DocumentAnalysisValidationError) as caught:
        await ClaudeDocumentAnalysisAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert client.query_count == 1 and caught.value.runtime.correction_attempt_count == 1
    assert caught.value.runtime.failure_stage == "pydantic_schema_validation"
    assert caught.value.runtime.pydantic_errors == ({"location": "product_candidates.0.raw_name", "type": "string_too_long"},)
    assert caught.value.runtime.structured_output_present
    assert caught.value.runtime.output_collection_counts["product_candidates"] == 1
    assert "x" * 100 not in repr(caught.value.runtime)


@pytest.mark.asyncio
async def test_noncorrectable_identity_and_missing_grounding_are_not_retried():
    """Never correct trusted-scope violations or absent document inspection."""

    value = request(); mismatch = finalization_submission(value); mismatch["organization_id"] = str(uuid.uuid4())
    async def submit_mismatch(options):
        ground_runtime(options)
        await options._maximor_adapters[-1].handler({"result": mismatch})
    client = FakeClient(messages=[], on_query=submit_mismatch)
    with pytest.raises(DocumentAnalysisValidationError) as caught:
        await ClaudeDocumentAnalysisAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert client.query_count == 1 and caught.value.runtime.failure_stage == "trusted_identity_mismatch"

    async def submit_without_grounding(options):
        await options._maximor_adapters[-1].handler({"result": finalization_submission(value)})
    client = FakeClient(messages=[], on_query=submit_without_grounding)
    with pytest.raises(DocumentAnalysisValidationError) as caught:
        await ClaudeDocumentAnalysisAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert client.query_count == 1 and "overview_not_retrieved" in caught.value.runtime.validation_issue_codes


@pytest.mark.asyncio
async def test_usage_accumulates_cache_classes_and_keeps_sdk_total_cost():
    """Separate cache tokens, accumulate response usage, and trust the final SDK cost."""

    value = request()
    terminal = sdk.ResultMessage(subtype="success",duration_ms=1,duration_api_ms=1,is_error=False,num_turns=2,session_id="s",terminal_reason="completed",total_cost_usd=0.15,usage={"input_tokens":5,"cache_creation_input_tokens":11,"cache_read_input_tokens":24,"output_tokens":11},model_usage={"model-a":{"inputTokens":5,"outputTokens":11,"cacheReadInputTokens":24,"cacheCreationInputTokens":11,"costUSD":0.15}})
    execution = await ClaudeDocumentAnalysisAgent(settings(),client_factory=factory_for(FakeClient(messages=[terminal],on_query=submit_finalization(value)))).execute(value,Tools())
    runtime=execution.runtime
    assert (runtime.input_tokens,runtime.cache_creation_input_tokens,runtime.cache_read_input_tokens,runtime.output_tokens)==(5,11,24,11)
    assert runtime.turn_count==2 and runtime.cost_usd==0.15
    assert runtime.model_usage["model-a"]["inputTokens"]==5
    assert runtime.model_usage["model-a"]["costUSD"] == pytest.approx(0.15)


@pytest.mark.asyncio
@pytest.mark.parametrize(("terminal_reason", "status", "expected"), [
    ("max_turns", None, "document_analysis_max_turns"),
    ("timeout", None, "document_analysis_timeout"),
    ("budget", None, "document_analysis_budget_exceeded"),
    ("error", 401, "document_analysis_authentication_failed"),
])
async def test_terminal_failures_remain_safe(terminal_reason, status, expected):
    """Map terminal SDK outcomes without raw exception or message disclosure."""

    terminal = sdk.ResultMessage(subtype="error", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1, session_id="s", terminal_reason=terminal_reason, api_error_status=status)
    client = FakeClient(messages=[terminal])
    with pytest.raises(DocumentAnalysisRuntimeError) as caught:
        await ClaudeDocumentAnalysisAgent(settings(), client_factory=factory_for(client)).execute(request(), Tools())
    assert caught.value.code == expected and client.disconnected
