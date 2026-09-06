"""Exercise the isolated Claude SDK client transport and SKU-mapping adapters locally.

Mirrors `test_claude_document_analysis_agent.py`'s FakeClient-injection pattern:
no live API call, no network activity, no subprocess.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import claude_agent_sdk as sdk
import pytest

from maximor.config import DatabaseSettings
from maximor.document_analysis.schemas import CommercialStatus, EvidenceReference, EvidenceRepresentation, ExtractionSource
from maximor.sku_mapping.agent import (
    MCP_SERVER_NAME,
    MCP_TOOL_NAMES,
    FINALIZER_TOOL_NAME,
    TOOL_NAMES,
    TRUSTED_IDENTITY_FIELDS,
    ClaudeSkuMappingAgent,
)
from maximor.sku_mapping.contracts import SkuMappingTask
from maximor.sku_mapping.errors import SkuMappingConfigurationError, SkuMappingRuntimeError, SkuMappingValidationError
from maximor.sku_mapping.schemas import (
    RetrievedSku,
    SkuMappingOutcome,
    SkuMatchSource,
    SkuRecord,
    SkuRetrievalResult,
)
from maximor.sku_mapping.versions import (
    HYBRID_SKU_RETRIEVER_VERSION,
    SKU_MAPPING_TASK_SCHEMA_VERSION,
    SKU_RETRIEVAL_SCHEMA_VERSION,
)


def task() -> SkuMappingTask:
    """Return a generated fixed SKU-mapping task."""

    preprocessing_run_id = uuid.uuid4()
    evidence = EvidenceReference(
        preprocessing_run_id=preprocessing_run_id, page_number=1, block_id="native:p0001:b000000",
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    )
    return SkuMappingTask(
        schema_version=SKU_MAPPING_TASK_SCHEMA_VERSION, organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=preprocessing_run_id, analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="1", document_analysis_agent_version="a1",
        candidate_id="candidate-0001", raw_name="Premium Support",
        commercial_status=CommercialStatus.PURCHASED, candidate_evidence=(evidence,),
    )


def settings(key: str | None = "secret-test-key", **overrides) -> DatabaseSettings:
    """Return test-only centralized configuration."""

    values = dict(database_url="postgresql+asyncpg://test", anthropic_api_key=key, sku_mapping_project_root="..")
    values.update(overrides)
    return DatabaseSettings(**values)


def sku(task_value: SkuMappingTask) -> SkuRecord:
    """Return one generated authoritative catalog SKU for the given task's tenant."""

    return SkuRecord(
        id=uuid.uuid4(), source_sku_id=uuid.uuid4(), organization_id=task_value.organization_id,
        catalog_version_id=uuid.uuid4(), sku_code="PREMIUM_SUPPORT", name="Premium Support",
    )


def retrieval_result(task_value: SkuMappingTask, sku_value: SkuRecord) -> SkuRetrievalResult:
    """Return one generated ranked retrieval naming the given SKU as the top match."""

    retrieved = RetrievedSku(sku=sku_value, score=1.0, matched_sources=(SkuMatchSource.EXACT,), matched_text=sku_value.name)
    return SkuRetrievalResult(
        schema_version=SKU_RETRIEVAL_SCHEMA_VERSION, retriever_version=HYBRID_SKU_RETRIEVER_VERSION,
        organization_id=task_value.organization_id, catalog_version_id=sku_value.catalog_version_id,
        catalog_version_identifier="v1", candidate_id=task_value.candidate_id, candidates=(retrieved,),
    )


def valid_decision_payload(task_value: SkuMappingTask, sku_value: SkuRecord) -> dict:
    """Return the smallest schema-valid MATCH submission for the given task/SKU.

    Cites evidence by identifier (`evidence_ids`), matching what the real
    finalizer schema now accepts — not a full `EvidenceReference` object.
    """

    reference = task_value.candidate_evidence[0]
    evidence_id = reference.block_id or reference.table_id
    return {
        "outcome": "match", "sku_id": str(sku_value.id), "sku_code": sku_value.sku_code,
        "sku_name": sku_value.name, "evidence_ids": [evidence_id],
    }


class Tools:
    """Generated SkuMappingToolset recording only scope-bound inputs."""

    def __init__(self, retrieval: SkuRetrievalResult | None = None, authoritative: SkuRecord | None = None) -> None:
        self.calls = []
        self._retrieval = retrieval
        self._authoritative = authoritative

    async def retrieve_skus(self, value):
        self.calls.append(value)
        return self._retrieval

    async def get_authoritative_sku(self, value):
        self.calls.append(value)
        return self._authoritative

    async def get_evidence_region(self, value):
        self.calls.append(value)
        return {"ok": True}


def ground_runtime_object(runtime, sku_value: SkuRecord) -> None:
    """Simulate one successful retrieval and authoritative confirmation for this invocation."""

    if runtime.tool_call_count == 0:
        runtime.tool_call_count = 2
        runtime.tool_calls_by_name = {"retrieve_skus": 1, "get_authoritative_sku": 1}
        runtime.successful_tool_call_order = ("retrieve_skus", "get_authoritative_sku")
    runtime.catalog_version_id = sku_value.catalog_version_id
    runtime.authoritative_skus_by_id[sku_value.id] = sku_value


def ground_runtime(options, sku_value: SkuRecord) -> None:
    """Simulate one successful retrieval and authoritative confirmation for this invocation."""

    ground_runtime_object(getattr(options, "_maximor_runtime"), sku_value)


def submit_finalization(task_value: SkuMappingTask, sku_value: SkuRecord, payload: dict | None = None):
    """Return an async fake callback that grounds then submits one decision."""

    async def callback(options):
        ground_runtime(options, sku_value)
        return await options._maximor_adapters[-1].handler(
            {"decision": payload or valid_decision_payload(task_value, sku_value)},
        )

    return callback


class FakeClient:
    """Simulate the installed stateful client without subprocess or network activity."""

    def __init__(self, options=None, *, messages=(), connect_error=None, status_error=None, tools=TOOL_NAMES, mcp_servers=None, on_query=None, response_delay_seconds=0.0):
        self.options, self.messages = options, list(messages)
        self.query_count = 0
        self.connect_error, self.status_error, self.tools, self.mcp_servers, self.on_query = connect_error, status_error, tools, mcp_servers, on_query
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
        if self.on_query:
            value = self.on_query(self.options)
            if asyncio.iscoroutine(value):
                await value

    async def receive_response(self):
        if self.response_delay_seconds:
            await asyncio.sleep(self.response_delay_seconds)
        for message in self.messages: yield message

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

    with pytest.raises(SkuMappingConfigurationError) as caught:
        await ClaudeSkuMappingAgent(settings(None)).decide(task(), Tools())
    assert "secret" not in str(caught.value).lower()


def test_options_have_exact_namespaced_tools_and_project_skill():
    """Inspect locally created isolated SDK options and the in-process server."""

    options = ClaudeSkuMappingAgent(settings()).build_options(task(), Tools())
    assert tuple(options.allowed_tools) == MCP_TOOL_NAMES
    assert options.tools == [] and options.strict_mcp_config
    assert options.skills == ["sku-mapping"] and options.setting_sources == ["project"]
    assert set(options.mcp_servers) == {MCP_SERVER_NAME}
    assert options.mcp_servers[MCP_SERVER_NAME]["type"] == "sdk"
    assert options.plugins == [] and "Bash" in options.disallowed_tools
    assert options.max_thinking_tokens == 2048
    assert "secret-test-key" not in repr(getattr(options, "_maximor_runtime"))


def test_prompt_explains_evidence_id_citation():
    """Instruct citation by block/table identifier, not a full evidence object.

    Regression test: a real live run against Claude discovered that asking
    the agent to submit a full `EvidenceReference` object (`representation`,
    `extraction_source`, `bounding_box`, ...) reconstructed from a compact
    prompt summary is unreliable — a genuinely correct citation still failed
    exact-equality against the task's stored evidence. Citing by identifier
    and resolving server-side (`_resolve_evidence_ids`) makes that class of
    failure structurally impossible instead of relying on faithful
    reconstruction.
    """

    value = task()
    prompt = ClaudeSkuMappingAgent._prompt(value)
    assert "evidence_ids" in prompt
    assert value.candidate_evidence[0].block_id in prompt


def test_resolve_evidence_ids_returns_exact_task_evidence():
    """Resolve a submitted identifier back to the task's own exact EvidenceReference."""

    value = task()
    reference = value.candidate_evidence[0]
    resolved = ClaudeSkuMappingAgent._resolve_evidence_ids(value, [reference.block_id])
    assert resolved == [reference.model_dump(mode="json")]


@pytest.mark.parametrize("evidence_ids", [["native:p0001:b999999"], [], "not-a-list", None])
def test_resolve_evidence_ids_rejects_anything_unresolvable(evidence_ids):
    """Return None (never a partial list) for an unknown, empty, or malformed identifier list."""

    assert ClaudeSkuMappingAgent._resolve_evidence_ids(task(), evidence_ids) is None


def test_finalizer_schema_excludes_all_trusted_fields():
    """Keep identity/catalog-version fields application-bound while retaining definitions."""

    schema = ClaudeSkuMappingAgent._finalizer_input_schema()
    decision = schema["properties"]["decision"]
    assert "$defs" in schema
    for field_name in TRUSTED_IDENTITY_FIELDS:
        assert field_name not in decision["properties"]
    assert "evidence" not in decision["properties"]
    assert decision["properties"]["evidence_ids"]["type"] == "array"
    assert "evidence_ids" in decision["required"]


@pytest.mark.parametrize(("message", "expected"), [
    ("a match decision requires sku_id, sku_code, and sku_name together", "match_missing_sku_fields"),
    ("only a match decision may carry sku_id, sku_code, or sku_name", "non_match_carries_sku_fields"),
    ("an ambiguous decision must list at least two considered SKUs", "ambiguous_missing_considered_skus"),
    ("considered_sku_ids must not repeat", "considered_skus_duplicated"),
    ("only an ambiguous decision may carry considered_sku_ids", "non_ambiguous_carries_considered_skus"),
    ("a SKU-mapping decision requires at least one evidence reference", "decision_missing_evidence"),
    ("decision evidence must belong to the decision's preprocessing run", "decision_evidence_run_mismatch"),
])
def test_known_root_validation_messages_map_to_safe_codes(message, expected):
    """Expose stable root-validator codes without retaining validator values."""

    assert ClaudeSkuMappingAgent._map_pydantic_error({"type": "value_error", "msg": message}) == expected


@pytest.mark.asyncio
async def test_configured_in_process_server_lists_exactly_four_tools_before_connect():
    """Discover all three readers plus finalizer without a client session."""

    options = ClaudeSkuMappingAgent(settings()).build_options(task(), Tools())
    names = await ClaudeSkuMappingAgent._configured_mcp_tool_names(options)
    assert names == tuple(sorted(TOOL_NAMES))


@pytest.mark.asyncio
async def test_adapters_are_directly_invocable_and_scope_bound():
    """Call three readers and the in-memory finalizer without a client connection."""

    value = task()
    target = sku(value)
    tools = Tools(retrieval=retrieval_result(value, target), authoritative=target)
    adapters, runtime = ClaudeSkuMappingAgent(settings())._build_adapters(value, tools)
    evidence_arg = {"evidence": value.candidate_evidence[0].model_dump(mode="json")}

    retrieve_response = await adapters[0].handler({})
    authoritative_response = await adapters[1].handler({"sku_id": str(target.id)})
    evidence_response = await adapters[2].handler(evidence_arg)

    assert not retrieve_response.get("is_error")
    assert not authoritative_response.get("is_error")
    assert not evidence_response.get("is_error")
    assert len(tools.calls) == 3 == runtime.tool_call_count
    assert runtime.catalog_version_id == target.catalog_version_id
    assert runtime.authoritative_skus_by_id[target.id] == target

    rejected = await adapters[2].handler({**evidence_arg, "organization_id": str(uuid.uuid4())})
    assert rejected["is_error"] and runtime.failed_tool_call_count == 1
    assert runtime.adapter_invocations[-1]["schema_validation_reached"] is False
    assert runtime.adapter_invocations[-1]["failure_category"] == "schema_rejected"

    final_response = await adapters[-1].handler({"decision": valid_decision_payload(value, target)})
    assert not final_response.get("is_error")
    assert runtime.finalization_accepted


@pytest.mark.asyncio
async def test_finalizer_rejects_invalid_and_ungrounded_submissions_safely():
    """Return only safe codes and locations, never rejected semantic values."""

    value = task()
    target = sku(value)
    adapters, runtime = ClaudeSkuMappingAgent(settings())._build_adapters(value, Tools())
    ground_runtime_object(runtime, target)
    invalid = valid_decision_payload(value, target)
    invalid["outcome"] = "not_a_real_outcome"
    response = await adapters[-1].handler({"decision": invalid})
    assert response["is_error"] and runtime.failure_stage == "pydantic_schema_validation"
    assert runtime.correction_attempt_count == 1

    fresh_adapters, _ = ClaudeSkuMappingAgent(settings())._build_adapters(value, Tools())
    ungrounded = await fresh_adapters[-1].handler({"decision": valid_decision_payload(value, target)})
    assert ungrounded["is_error"]  # retrieve_skus was never actually called on this instance


@pytest.mark.asyncio
async def test_client_lifecycle_confirms_mcp_discovery_and_streams_output():
    """Verify connect, MCP status, prompt, stream, and disconnect through a fake client."""

    value = task()
    target = sku(value)
    assistant = sdk.AssistantMessage(content=[], model="test")
    terminal = sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="s", terminal_reason="completed")
    client = FakeClient(messages=[assistant, terminal], on_query=submit_finalization(value, target))
    execution = await ClaudeSkuMappingAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert client.connected and client.queried and client.disconnected
    assert execution.runtime.mcp_server_status == "connected"
    assert execution.runtime.announced_tool_names == tuple(sorted(TOOL_NAMES))
    assert execution.decision.candidate_id == value.candidate_id
    assert execution.decision.outcome is SkuMappingOutcome.MATCH
    assert execution.decision.sku_id == target.id
    assert execution.decision.catalog_version_id == target.catalog_version_id
    assert execution.runtime.session_initialization_succeeded is True
    assert execution.runtime.terminal_kind == "accepted_by_finalizer"
    assert execution.runtime.total_elapsed_ms is not None and execution.runtime.total_elapsed_ms >= 0


@pytest.mark.asyncio
async def test_explicit_failed_sdk_mcp_status_remains_fatal():
    """Reject an SDK-reported failed server even though local registration exists."""

    client = FakeClient(mcp_servers=[{"name": MCP_SERVER_NAME, "status": "failed"}])
    with pytest.raises(SkuMappingRuntimeError) as caught:
        await ClaudeSkuMappingAgent(settings(), client_factory=factory_for(client)).execute(task(), Tools())
    assert caught.value.code == "sku_mapping_mcp_initialization_failed" and client.disconnected


@pytest.mark.asyncio
async def test_incorrect_local_registration_is_fatal_before_client_connect():
    """Fail closed if the configured in-process server does not expose all tools."""

    class BadLocalAgent(ClaudeSkuMappingAgent):
        def build_options(self, value, tools):
            options = super().build_options(value, tools)
            options.mcp_servers[MCP_SERVER_NAME] = sdk.create_sdk_mcp_server(MCP_SERVER_NAME, tools=[])
            return options

    client = FakeClient()
    with pytest.raises(SkuMappingRuntimeError) as caught:
        await BadLocalAgent(settings(), client_factory=factory_for(client)).execute(task(), Tools())
    assert caught.value.code == "sku_mapping_mcp_tool_discovery_failed"
    assert not client.connected and client.disconnected


@pytest.mark.asyncio
async def test_one_correctable_schema_failure_then_success_uses_same_session():
    """Send one value-free correction and accept only the corrected valid decision."""

    value = task()
    target = sku(value)
    invalid = valid_decision_payload(value, target)
    invalid["outcome"] = "not_a_real_outcome"

    async def submit_then_correct(options):
        ground_runtime(options, target)
        finalizer = options._maximor_adapters[-1]
        first = await finalizer.handler({"decision": invalid})
        second = await finalizer.handler({"decision": valid_decision_payload(value, target)})
        assert first["is_error"] and not second.get("is_error")

    client = FakeClient(messages=[], on_query=submit_then_correct)
    execution = await ClaudeSkuMappingAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert client.query_count == 1 and execution.runtime.correction_attempt_count == 1
    assert execution.runtime.failure_stage is None
    assert execution.decision.outcome is SkuMappingOutcome.MATCH


@pytest.mark.asyncio
async def test_second_invalid_output_stops_without_third_request():
    """Bound schema correction to one attempt across the same client session."""

    value = task()
    target = sku(value)
    invalid = valid_decision_payload(value, target)
    invalid["outcome"] = "not_a_real_outcome"

    async def submit_twice(options):
        ground_runtime(options, target)
        finalizer = options._maximor_adapters[-1]
        await finalizer.handler({"decision": invalid})
        await finalizer.handler({"decision": invalid})

    client = FakeClient(messages=[], on_query=submit_twice)
    with pytest.raises(SkuMappingValidationError) as caught:
        await ClaudeSkuMappingAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert client.query_count == 1 and caught.value.runtime.correction_attempt_count == 1
    assert caught.value.runtime.failure_stage == "pydantic_schema_validation"
    assert caught.value.runtime.structured_output_present


@pytest.mark.asyncio
async def test_noncorrectable_identity_override_and_missing_retrieval_are_not_retried():
    """Never correct trusted-scope violations or a decision with no successful retrieval."""

    value = task()
    target = sku(value)
    mismatch = valid_decision_payload(value, target)
    mismatch["organization_id"] = str(uuid.uuid4())

    async def submit_mismatch(options):
        ground_runtime(options, target)
        await options._maximor_adapters[-1].handler({"decision": mismatch})

    client = FakeClient(messages=[], on_query=submit_mismatch)
    with pytest.raises(SkuMappingValidationError) as caught:
        await ClaudeSkuMappingAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert client.query_count == 1 and caught.value.runtime.failure_stage == "trusted_identity_mismatch"

    async def submit_without_retrieval(options):
        # Ground only catalog_version_id (so the decision is schema-valid) while leaving
        # tool_calls_by_name empty, isolating the validation-layer "no retrieval" check
        # from the earlier pydantic-schema-layer "missing trusted field" failure.
        getattr(options, "_maximor_runtime").catalog_version_id = target.catalog_version_id
        await options._maximor_adapters[-1].handler({"decision": valid_decision_payload(value, target)})

    client2 = FakeClient(messages=[], on_query=submit_without_retrieval)
    with pytest.raises(SkuMappingValidationError) as caught2:
        await ClaudeSkuMappingAgent(settings(), client_factory=factory_for(client2)).execute(value, Tools())
    assert client2.query_count == 1
    assert "retrieval_not_performed" in caught2.value.runtime.validation_issue_codes


@pytest.mark.asyncio
async def test_usage_accumulates_cache_classes_and_keeps_sdk_total_cost():
    """Separate cache tokens, accumulate response usage, and trust the final SDK cost."""

    value = task()
    target = sku(value)
    terminal = sdk.ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="s",
        terminal_reason="completed", total_cost_usd=0.05,
        usage={"input_tokens": 3, "cache_creation_input_tokens": 4, "cache_read_input_tokens": 9, "output_tokens": 6},
        model_usage={"model-a": {"inputTokens": 3, "outputTokens": 6, "cacheReadInputTokens": 9, "cacheCreationInputTokens": 4, "costUSD": 0.05}},
    )
    client = FakeClient(messages=[terminal], on_query=submit_finalization(value, target))
    execution = await ClaudeSkuMappingAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    runtime = execution.runtime
    assert (runtime.input_tokens, runtime.cache_creation_input_tokens, runtime.cache_read_input_tokens, runtime.output_tokens) == (3, 4, 9, 6)
    assert runtime.turn_count == 2 and runtime.cost_usd == 0.05
    assert runtime.model_usage["model-a"]["costUSD"] == pytest.approx(0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize(("terminal_reason", "status", "expected"), [
    ("max_turns", None, "sku_mapping_max_turns"),
    ("timeout", None, "sku_mapping_timeout"),
    ("budget", None, "sku_mapping_budget_exceeded"),
    ("error", 401, "sku_mapping_authentication_failed"),
])
async def test_terminal_failures_remain_safe(terminal_reason, status, expected):
    """Map terminal SDK outcomes without raw exception or message disclosure."""

    terminal = sdk.ResultMessage(subtype="error", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1, session_id="s", terminal_reason=terminal_reason, api_error_status=status)
    client = FakeClient(messages=[terminal])
    with pytest.raises(SkuMappingRuntimeError) as caught:
        await ClaudeSkuMappingAgent(settings(), client_factory=factory_for(client)).execute(task(), Tools())
    assert caught.value.code == expected and client.disconnected


@pytest.mark.asyncio
async def test_real_sdk_timeout_is_mapped_safely():
    """Map a real asyncio timeout without hanging or disclosing internals."""

    client = FakeClient(messages=[], response_delay_seconds=0.05)
    with pytest.raises(SkuMappingRuntimeError) as caught:
        await ClaudeSkuMappingAgent(
            settings(sku_mapping_timeout_seconds=0.01), client_factory=factory_for(client),
        ).execute(task(), Tools())
    assert caught.value.code == "sku_mapping_timeout"
