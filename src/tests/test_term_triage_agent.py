"""Exercise the isolated Claude SDK client transport and term-triage adapters locally.

Mirrors the sibling agents' FakeClient-injection pattern: no live API call,
no network activity, no subprocess, no supplied PDFs/ground truth.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import claude_agent_sdk as sdk
import pytest

from maximor.config import DatabaseSettings
from maximor.document_analysis.schemas import CommercialStatus
from maximor.term_triage.agent import (
    MCP_SERVER_NAME,
    MCP_TOOL_NAMES,
    FINALIZER_TOOL_NAME,
    TOOL_NAMES,
    TRUSTED_IDENTITY_FIELDS,
    ClaudeTermTriageAgent,
)
from maximor.term_triage.contracts import TermTriageTask, TriageCandidateContext, TriageTermContext
from maximor.term_triage.errors import TermTriageConfigurationError, TermTriageRuntimeError


def task() -> TermTriageTask:
    """Return a generated fixed term-triage task: two terms, one candidate."""

    return TermTriageTask(
        schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=uuid.uuid4(), analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="1", document_analysis_agent_version="a1",
        candidates=(TriageCandidateContext(candidate_id="candidate-0001", raw_name="Premium Support", commercial_status=CommercialStatus.PURCHASED),),
        terms=(
            TriageTermContext(term_id="term-0001", raw_name="Billing contact", raw_value="Jane Doe"),
            TriageTermContext(term_id="term-0002", raw_name="Payment terms", raw_value="Net 45"),
        ),
    )


def settings(key: str | None = "secret-test-key", **overrides) -> DatabaseSettings:
    """Return test-only centralized configuration."""

    values = dict(database_url="postgresql+asyncpg://test", anthropic_api_key=key, term_triage_project_root="..")
    values.update(overrides)
    return DatabaseSettings(**values)


def full_coverage_payload() -> dict:
    return {"decisions": [
        {"term_id": "term-0001", "disposition": "document_metadata"},
        {"term_id": "term-0002", "disposition": "potential_line_item"},
    ]}


def submit_finalization(payload: dict):
    """Return an async fake callback that submits one batch of triage decisions."""

    async def callback(options):
        return await options._maximor_adapters[-1].handler({"result": payload})

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

    with pytest.raises(TermTriageConfigurationError) as caught:
        ClaudeTermTriageAgent(settings(None)).build_options(task())
    assert "secret-test-key" not in repr(caught.value)


def test_options_have_exact_namespaced_finalizer_only_tool_and_project_skill():
    """No read-only tools exist at all; only the one namespaced finalizer."""

    options = ClaudeTermTriageAgent(settings()).build_options(task())
    assert tuple(options.allowed_tools) == MCP_TOOL_NAMES == (f"mcp__{MCP_SERVER_NAME}__{FINALIZER_TOOL_NAME}",)
    assert options.tools == [] and options.strict_mcp_config
    assert options.skills == ["term-triage"] and options.setting_sources == ["project"]
    assert set(options.mcp_servers) == {MCP_SERVER_NAME}
    assert "Bash" in options.disallowed_tools and "WebSearch" in options.disallowed_tools
    assert "secret-test-key" not in repr(getattr(options, "_maximor_runtime"))


def test_finalizer_schema_excludes_trusted_fields():
    schema = ClaudeTermTriageAgent._finalizer_input_schema()
    result = schema["properties"]["result"]
    for field_name in TRUSTED_IDENTITY_FIELDS:
        assert field_name not in result["properties"]


@pytest.mark.asyncio
async def test_finalizer_accepts_full_coverage_batch():
    value = task()
    adapters, runtime = ClaudeTermTriageAgent(settings())._build_adapters(value)
    response = await adapters[-1].handler({"result": full_coverage_payload()})
    assert not response.get("is_error"), response
    assert runtime.finalization_accepted
    result = runtime._finalization_capture.accepted_result
    assert len(result.decisions) == 2


@pytest.mark.asyncio
async def test_finalizer_rejects_incomplete_coverage_and_allows_one_correction():
    value = task()
    adapters, runtime = ClaudeTermTriageAgent(settings())._build_adapters(value)
    partial = {"decisions": [{"term_id": "term-0001", "disposition": "document_metadata"}]}
    first = await adapters[-1].handler({"result": partial})
    assert first["is_error"]
    assert runtime.correction_attempt_count == 1
    second = await adapters[-1].handler({"result": full_coverage_payload()})
    assert not second.get("is_error")
    third = await adapters[-1].handler({"result": partial})
    assert third["is_error"]
    assert runtime._finalization_capture.rejection_event.is_set()


@pytest.mark.asyncio
async def test_finalizer_rejects_trusted_identity_override():
    value = task()
    adapters, runtime = ClaudeTermTriageAgent(settings())._build_adapters(value)
    payload = {**full_coverage_payload(), "organization_id": str(uuid.uuid4())}
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"] and runtime.failure_stage == "trusted_identity_mismatch"
    assert runtime.correction_attempt_count == 0


@pytest.mark.asyncio
async def test_finalization_accepted_end_to_end_through_execute():
    value = task()
    client = FakeClient(messages=[], on_query=submit_finalization(full_coverage_payload()))
    execution = await ClaudeTermTriageAgent(settings(), client_factory=factory_for(client)).execute(value)
    assert len(execution.result.decisions) == 2
    assert execution.runtime.finalization_accepted
    assert client.connected and client.disconnected


@pytest.mark.asyncio
async def test_timeout_raises_safe_runtime_error():
    value = task()
    client = FakeClient(messages=[], response_delay_seconds=0.05)
    with pytest.raises(TermTriageRuntimeError) as caught:
        await ClaudeTermTriageAgent(
            settings(term_triage_timeout_seconds=0.01), client_factory=factory_for(client),
        ).execute(value)
    assert caught.value.code == "term_triage_timeout"
    assert caught.value.runtime.terminal_kind == "timeout"


@pytest.mark.asyncio
async def test_missing_finalizer_call_raises_validation_error():
    value = task()
    client = FakeClient(messages=[sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s")])
    with pytest.raises(TermTriageRuntimeError) as caught:
        await ClaudeTermTriageAgent(settings(), client_factory=factory_for(client)).execute(value)
    assert caught.value.code == "term_triage_finalizer_not_called"


@pytest.mark.asyncio
async def test_connect_transport_failure_is_safe_and_distinct():
    value = task()
    client = FakeClient(connect_error=RuntimeError("boom"))
    with pytest.raises(TermTriageRuntimeError) as caught:
        await ClaudeTermTriageAgent(settings(), client_factory=factory_for(client)).execute(value)
    assert caught.value.code == "term_triage_sdk_initialization_failed"
    assert "boom" not in repr(caught.value)


def test_persistence_diagnostics_never_carry_raw_text_or_secrets():
    value = task()
    adapters, runtime = ClaudeTermTriageAgent(settings())._build_adapters(value)
    diagnostics = runtime.persistence_diagnostics()
    blob = repr(diagnostics).lower()
    for forbidden in ("secret-test-key", "billing contact", "jane doe", "select ", "bearer "):
        assert forbidden not in blob
