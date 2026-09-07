"""Exercise the isolated Claude SDK client transport and term-applicability adapters locally.

Mirrors `test_claude_document_analysis_agent.py`/`test_sku_mapping_agent.py`'s
FakeClient-injection pattern: no live API call, no network activity, no
subprocess, no supplied PDFs/ground truth.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import claude_agent_sdk as sdk
import pytest

from maximor.config import DatabaseSettings
from maximor.document_analysis.schemas import CommercialStatus, EvidenceReference, EvidenceRepresentation, ExtractionSource
from maximor.term_applicability.agent import (
    MCP_SERVER_NAME,
    MCP_TOOL_NAMES,
    FINALIZER_TOOL_NAME,
    TOOL_NAMES,
    TRUSTED_IDENTITY_FIELDS,
    ClaudeTermApplicabilityAgent,
)
from maximor.term_applicability.contracts import CandidateContext, TermApplicabilityTask, TermContext
from maximor.term_applicability.errors import (
    TermApplicabilityConfigurationError,
    TermApplicabilityRuntimeError,
    TermApplicabilityValidationError,
)
from maximor.term_applicability.schemas import ApplicabilityScope, RawCommercialFactField, TermDisposition


def task() -> TermApplicabilityTask:
    """Return a generated fixed term-applicability task: one term, one candidate."""

    preprocessing_run_id = uuid.uuid4()
    return TermApplicabilityTask(
        schema_version="1.0.0", organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=preprocessing_run_id, analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="1", document_analysis_agent_version="a1",
        candidates=(CandidateContext(candidate_id="candidate-0001", raw_name="Premium Support", commercial_status=CommercialStatus.PURCHASED, evidence_ids=("native:p0001:b000001",)),),
        terms=(TermContext(term_id="term-0001", raw_name="Payment terms", raw_value="Net 45", evidence_ids=("native:p0001:b000000",)),),
        evidence_by_id={
            "native:p0001:b000000": EvidenceReference(
                preprocessing_run_id=preprocessing_run_id, page_number=1, block_id="native:p0001:b000000",
                representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
            ),
            "native:p0001:b000001": EvidenceReference(
                preprocessing_run_id=preprocessing_run_id, page_number=1, block_id="native:p0001:b000001",
                representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
            ),
        },
    )


def task_with_expected_fact_fields() -> TermApplicabilityTask:
    """Return the base task with candidate-0001 raw attributes that hint at two expected fields."""

    base = task()
    return TermApplicabilityTask(**{
        **base.model_dump(),
        "candidates": (
            CandidateContext(
                candidate_id="candidate-0001", raw_name="Premium Support",
                commercial_status=CommercialStatus.PURCHASED,
                raw_attributes={"qty": "5", "unit_price": "$10.00"},
                evidence_ids=("native:p0001:b000001",),
            ),
        ),
    })


def task_with_excluded_candidate() -> TermApplicabilityTask:
    """Return the base task plus one additional candidate ineligible for facts."""

    base = task()
    return TermApplicabilityTask(**{
        **base.model_dump(),
        "candidates": (*base.candidates, CandidateContext(
            candidate_id="candidate-0002", raw_name="Excluded Add-on",
            commercial_status=CommercialStatus.EXCLUDED, evidence_ids=("native:p0001:b000001",),
        )),
    })


def settings(key: str | None = "secret-test-key", **overrides) -> DatabaseSettings:
    """Return test-only centralized configuration."""

    values = dict(database_url="postgresql+asyncpg://test", anthropic_api_key=key, term_applicability_project_root="..")
    values.update(overrides)
    return DatabaseSettings(**values)


class Tools:
    """Generated TermApplicabilityToolset recording only scope-bound inputs."""

    def __init__(self) -> None:
        self.calls = []

    async def get_term_evidence_region(self, value):
        self.calls.append(value)
        return {"ok": True}

    async def get_candidate_evidence_region(self, value):
        self.calls.append(value)
        return {"ok": True}

    async def get_evidence_region(self, value):
        self.calls.append(value)
        return {"ok": True}


def document_scope_payload() -> dict:
    return {"decisions": [{
        "term_id": "term-0001", "disposition": "line_item", "applicability_scope": "document",
        "evidence_ids": ["native:p0001:b000000"],
    }]}


def candidate_scope_payload() -> dict:
    return {"decisions": [{
        "term_id": "term-0001", "disposition": "line_item", "applicability_scope": "candidate",
        "applies_to_candidate_ids": ["candidate-0001"], "evidence_ids": ["native:p0001:b000000"],
    }]}


def unknown_scope_payload() -> dict:
    return {"decisions": [{
        "term_id": "term-0001", "disposition": "line_item", "applicability_scope": "unknown",
        "evidence_ids": ["native:p0001:b000000"],
    }]}


def document_metadata_payload() -> dict:
    return {"decisions": [{"term_id": "term-0001", "disposition": "document_metadata"}]}


def candidate_fact(candidate_id: str = "candidate-0001", field: str = "quantity", raw_value: str = "10", raw_period_label: str | None = None, evidence_ids: tuple[str, ...] = ("native:p0001:b000001",)) -> dict:
    fact = {"candidate_id": candidate_id, "field": field, "raw_value": raw_value, "evidence_ids": list(evidence_ids)}
    if raw_period_label is not None:
        fact["raw_period_label"] = raw_period_label
    return fact


def candidate_facts_payload(*facts: dict, candidate_id: str = "candidate-0001") -> dict:
    return {"candidate_commercial_facts": [{"candidate_id": candidate_id, "facts": list(facts)}]}


def coverage_payload(candidate_id: str = "candidate-0001", unresolved_fields: list[str] | None = None, evidence_ids: tuple[str, ...] = ("native:p0001:b000001",), **extra) -> dict:
    item = {"candidate_id": candidate_id, "unresolved_fields": list(unresolved_fields or []), "evidence_ids": list(evidence_ids)}
    item.update(extra)
    return {"candidate_commercial_fact_coverage": [item]}


def ground_runtime(options, *, ground_term: bool = True, ground_candidate: bool = False) -> None:
    """Simulate successful term/candidate evidence retrievals for this invocation."""

    runtime = getattr(options, "_maximor_runtime")
    if ground_term:
        runtime.record_term_evidence_retrieved("term-0001", "native:p0001:b000000")
    if ground_candidate:
        runtime.record_candidate_evidence_retrieved("candidate-0001", "native:p0001:b000001")


def submit_finalization(payload: dict, *, ground_term: bool = True, ground_candidate: bool = False):
    """Return an async fake callback that grounds then submits one batch of decisions."""

    async def callback(options):
        ground_runtime(options, ground_term=ground_term, ground_candidate=ground_candidate)
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

    with pytest.raises(TermApplicabilityConfigurationError) as caught:
        ClaudeTermApplicabilityAgent(settings(None)).build_options(task(), Tools())
    assert "secret-test-key" not in repr(caught.value)


def test_options_have_exact_namespaced_tools_and_project_skill():
    """Inspect locally created isolated SDK options and the in-process server."""

    options = ClaudeTermApplicabilityAgent(settings()).build_options(task(), Tools())
    assert tuple(options.allowed_tools) == MCP_TOOL_NAMES
    assert all(name.startswith(f"mcp__{MCP_SERVER_NAME}__") for name in options.allowed_tools)
    assert options.tools == [] and options.strict_mcp_config
    assert options.skills == ["term-applicability"] and options.setting_sources == ["project"]
    assert set(options.mcp_servers) == {MCP_SERVER_NAME}
    assert options.mcp_servers[MCP_SERVER_NAME]["type"] == "sdk"
    assert options.plugins == [] and "Bash" in options.disallowed_tools and "WebSearch" in options.disallowed_tools
    assert "secret-test-key" not in repr(getattr(options, "_maximor_runtime"))


def test_finalizer_submission_order_is_canonicalized_without_deduplication():
    """Server ordering removes mechanical correction failures but retains duplicates."""

    payload = {
        "decisions": [
            {"term_id": "term-0002", "evidence_ids": ["z", "a"]},
            {"term_id": "term-0001", "evidence_ids": ["b", "a"]},
            {"term_id": "term-0001", "evidence_ids": []},
        ],
        "candidate_commercial_facts": [],
    }
    normalized = ClaudeTermApplicabilityAgent._canonicalize_submission(payload)
    assert [item["term_id"] for item in normalized["decisions"]] == ["term-0001", "term-0001", "term-0002"]
    assert normalized["decisions"][2]["evidence_ids"] == ["a", "z"]


def test_grounding_feedback_is_scoped_and_value_free():
    """Correction feedback exposes only retrieved IDs for the affected term/candidate."""

    value = task()
    summary = type("Runtime", (), {
        "retrieved_term_evidence_ids": {"term-0001": frozenset({"native:p0001:b000000"})},
        "retrieved_candidate_evidence_ids": {"candidate-0001": frozenset({"native:p0001:b000001"})},
    })()
    payload = {
        "decisions": [{"term_id": "term-0001", "applicability_scope": "document", "evidence_ids": ["native:p0001:b999999"]}],
        "candidate_commercial_facts": [{"candidate_id": "candidate-0001", "facts": [{"evidence_ids": ["native:p0001:b888888"]}]}],
    }
    feedback = ClaudeTermApplicabilityAgent._grounding_correction_payload(value, payload, summary)
    assert {item["requirement"] for item in feedback} == {"term_evidence", "commercial_fact_evidence"}
    assert all(item["allowed_evidence_ids"] in (["native:p0001:b000000"], ["native:p0001:b000001"]) for item in feedback)
    assert "b999999" not in json.dumps(feedback)


def test_finalizer_schema_excludes_trusted_and_evidence_object_fields():
    """The finalizer schema never exposes trusted identity fields or a raw EvidenceReference shape."""

    schema = ClaudeTermApplicabilityAgent._finalizer_input_schema()
    result = schema["properties"]["result"]
    for field_name in TRUSTED_IDENTITY_FIELDS:
        assert field_name not in result["properties"]
    decision_definition = schema["$defs"]["TermApplicabilityDecision"]
    assert "schema_version" not in decision_definition["properties"]
    assert "evidence" not in decision_definition["properties"]
    assert decision_definition["properties"]["evidence_ids"]["type"] == "array"


@pytest.mark.asyncio
async def test_eight_adapters_are_directly_invocable_and_scope_bound():
    """Call both readers and the in-memory finalizer without a client connection."""

    value, tools = task(), Tools()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, tools)
    term_response = await adapters[0].handler({"term_id": "term-0001", "evidence_id": "native:p0001:b000000"})
    candidate_response = await adapters[1].handler({"candidate_id": "candidate-0001", "evidence_id": "native:p0001:b000001"})
    assert not term_response.get("is_error") and not candidate_response.get("is_error")
    assert len(tools.calls) == 2 == runtime.tool_call_count
    assert runtime.retrieved_term_evidence_ids == {"term-0001": frozenset({"native:p0001:b000000"})}
    assert runtime.retrieved_candidate_evidence_ids == {"candidate-0001": frozenset({"native:p0001:b000001"})}
    rejected = await adapters[0].handler({"term_id": "term-0001", "evidence_id": "native:p0001:b000000", "organization_id": str(uuid.uuid4())})
    assert rejected["is_error"] and runtime.failed_tool_call_count == 1
    final_response = await adapters[-1].handler({"result": document_metadata_payload()})
    assert not final_response.get("is_error")
    assert runtime.finalization_accepted


@pytest.mark.asyncio
async def test_finalizer_accepts_document_scope_decision():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=True)
    response = await adapters[-1].handler({"result": document_scope_payload()})
    assert not response.get("is_error"), response
    result = runtime._finalization_capture.accepted_result
    assert result.decisions[0].applicability_scope == ApplicabilityScope.DOCUMENT


@pytest.mark.asyncio
async def test_finalizer_accepts_candidate_scope_decision_with_both_retrievals():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=True, ground_candidate=True)
    response = await adapters[-1].handler({"result": candidate_scope_payload()})
    assert not response.get("is_error"), response
    result = runtime._finalization_capture.accepted_result
    assert result.decisions[0].applicability_scope == ApplicabilityScope.CANDIDATE
    assert result.decisions[0].applies_to_candidate_ids == ("candidate-0001",)


@pytest.mark.asyncio
async def test_finalizer_accepts_unknown_scope_decision():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=True)
    response = await adapters[-1].handler({"result": unknown_scope_payload()})
    assert not response.get("is_error"), response
    result = runtime._finalization_capture.accepted_result
    assert result.decisions[0].applicability_scope == ApplicabilityScope.UNKNOWN


@pytest.mark.asyncio
async def test_finalizer_accepts_document_metadata_decision_without_any_retrieval():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    response = await adapters[-1].handler({"result": document_metadata_payload()})
    assert not response.get("is_error"), response
    result = runtime._finalization_capture.accepted_result
    assert result.decisions[0].disposition == TermDisposition.DOCUMENT_METADATA


@pytest.mark.asyncio
async def test_finalizer_rejects_candidate_scope_without_candidate_evidence_retrieval():
    """A candidate attribution the runtime cannot support is rejected, correctable, not silently accepted."""

    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=True, ground_candidate=False)
    response = await adapters[-1].handler({"result": candidate_scope_payload()})
    assert response["is_error"]
    assert runtime.failure_stage == "evidence_validation_failure"
    assert runtime.correction_attempt_count == 1


@pytest.mark.asyncio
async def test_finalizer_rejects_evidence_never_retrieved_this_run():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    response = await adapters[-1].handler({"result": document_scope_payload()})
    assert response["is_error"]
    assert runtime.failure_stage == "evidence_validation_failure"


@pytest.mark.asyncio
async def test_finalizer_rejects_arbitrary_evidence_id_not_in_task():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    payload = {"decisions": [{"term_id": "term-0001", "disposition": "line_item", "applicability_scope": "unknown", "evidence_ids": ["native:p9999:b999999"]}]}
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"]
    assert runtime.failure_stage == "evidence_identifier_unresolved"
    assert runtime.correction_attempt_count == 1


@pytest.mark.asyncio
async def test_finalizer_rejects_trusted_identity_override():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    payload = {**document_metadata_payload(), "organization_id": str(uuid.uuid4())}
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"] and runtime.failure_stage == "trusted_identity_mismatch"
    assert runtime.correction_attempt_count == 0


@pytest.mark.asyncio
async def test_finalizer_rejects_decision_level_schema_version_override():
    """Claude cannot set a decision's schema_version through the finalizer either."""

    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    payload = {"decisions": [{"term_id": "term-0001", "disposition": "document_metadata", "schema_version": "9.9.9"}]}
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"]
    assert runtime.failure_stage == "evidence_identifier_unresolved"


@pytest.mark.asyncio
async def test_one_correctable_schema_failure_then_success_uses_same_session():
    """A correctable rejection allows exactly one further submission in the same session."""

    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    finalize = adapters[-1]
    bad = {"decisions": [{"term_id": "term-0001", "disposition": "line_item", "applicability_scope": "unknown", "evidence_ids": ["not-a-real-id"]}]}
    first = await finalize.handler({"result": bad})
    assert first["is_error"] and runtime.correction_attempt_count == 1
    runtime.record_term_evidence_retrieved("term-0001", "native:p0001:b000000")
    second = await finalize.handler({"result": document_scope_payload()})
    assert not second.get("is_error")
    third = await finalize.handler({"result": bad})
    assert third["is_error"]
    assert "correction_allowed\":false" in third["content"][0]["text"] or '"correction_allowed": false' in third["content"][0]["text"]


@pytest.mark.asyncio
async def test_second_invalid_output_stops_without_third_request():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    finalize = adapters[-1]
    bad = {"decisions": [{"term_id": "term-0001", "disposition": "line_item", "applicability_scope": "unknown", "evidence_ids": ["missing"]}]}
    await finalize.handler({"result": bad})
    second = await finalize.handler({"result": bad})
    assert second["is_error"]
    assert runtime._finalization_capture.rejection_event.is_set()


@pytest.mark.asyncio
async def test_noncorrectable_identity_mismatch_is_not_retried():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    finalize = adapters[-1]
    payload = {**document_metadata_payload(), "document_id": str(uuid.uuid4())}
    await finalize.handler({"result": payload})
    assert runtime.correction_attempt_count == 0
    assert runtime._finalization_capture.rejection_event.is_set()


@pytest.mark.asyncio
async def test_finalization_accepted_end_to_end_through_execute():
    """A full execute() run accepts a grounded submission through the fake SDK client."""

    value = task()
    client = FakeClient(messages=[], on_query=submit_finalization(document_scope_payload(), ground_term=True))
    execution = await ClaudeTermApplicabilityAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert execution.result.decisions[0].applicability_scope == ApplicabilityScope.DOCUMENT
    assert execution.runtime.finalization_accepted
    assert client.connected and client.disconnected


@pytest.mark.asyncio
async def test_timeout_raises_safe_runtime_error_with_partial_timing():
    value = task()
    client = FakeClient(messages=[], response_delay_seconds=0.05)
    with pytest.raises(TermApplicabilityRuntimeError) as caught:
        await ClaudeTermApplicabilityAgent(
            settings(term_applicability_timeout_seconds=0.01), client_factory=factory_for(client),
        ).execute(value, Tools())
    assert caught.value.code == "term_applicability_timeout"
    assert caught.value.runtime.terminal_kind == "timeout"
    assert caught.value.runtime.total_elapsed_ms is not None and caught.value.runtime.total_elapsed_ms >= 0


@pytest.mark.asyncio
async def test_timeout_after_evidence_tool_calls_preserves_tool_diagnostics():
    """A timeout reached after some successful evidence retrieval retains that tool history.

    Distinguishes "timed out before any tool call" (the prior test) from a
    session that made real progress: `last_successful_tool_return_at` and
    `successful_tool_call_order` must reflect the two retrievals that
    actually completed before the SDK response never arrived.
    """

    value = task()

    async def touch_evidence(options):
        term_adapter, candidate_adapter, _finalize = options._maximor_adapters
        await term_adapter.handler({"term_id": "term-0001", "evidence_id": "native:p0001:b000000"})
        await candidate_adapter.handler({"candidate_id": "candidate-0001", "evidence_id": "native:p0001:b000001"})

    client = FakeClient(messages=[], response_delay_seconds=0.05, on_query=touch_evidence)
    with pytest.raises(TermApplicabilityRuntimeError) as caught:
        await ClaudeTermApplicabilityAgent(
            settings(term_applicability_timeout_seconds=0.01), client_factory=factory_for(client),
        ).execute(value, Tools())
    runtime = caught.value.runtime
    assert caught.value.code == "term_applicability_timeout"
    assert runtime.terminal_kind == "timeout"
    assert runtime.successful_tool_call_order == ("get_term_evidence_region", "get_candidate_evidence_region")
    assert runtime.tool_call_count == 2
    assert runtime.last_successful_tool_return_at is not None
    assert runtime.last_successful_tool_return_elapsed_ms is not None
    # The interval after the last successful return is itself observable,
    # bounded, and never negative -- this is what lets a real failure be
    # distinguished from "hung immediately after the last tool returned".
    assert runtime.elapsed_after_last_successful_tool_return_ms is not None
    assert runtime.elapsed_after_last_successful_tool_return_ms >= 0
    assert runtime.finalization_submission_count == 0  # the finalizer was never reached


@pytest.mark.asyncio
async def test_timeout_after_one_correctable_finalizer_rejection_preserves_correction_state():
    """A timeout after one correctable rejection shows the finalizer was reached, corrected, not accepted.

    This single reachable state covers two of the requested checkpoints at
    once ("finalizer tool-use but before accepted finalization" and "after
    an in-session correction"): the guarded finalizer only ever grants a
    correction on a *correctable* rejection, and granting one is exactly
    what leaves the session able to keep running (and therefore able to
    time out) rather than ending immediately in `rejection_event` --
    a second attempt after the one allowed correction, or any
    non-correctable rejection, sets `rejection_event` and resolves as an
    immediate `TermApplicabilityValidationError` instead of a timeout (see
    `test_second_invalid_output_stops_without_third_request` and
    `test_noncorrectable_identity_mismatch_is_not_retried`). So "reached but
    not yet accepted" and "already used its one correction" are the same
    observable state here, not two independently reachable ones.
    """

    value = task()
    unresolvable = {"decisions": [{
        "term_id": "term-0001", "disposition": "line_item", "applicability_scope": "unknown",
        "evidence_ids": ["does-not-exist"],
    }]}

    async def reject_once(options):
        _term, _candidate, finalize = options._maximor_adapters
        await finalize.handler({"result": unresolvable})

    client = FakeClient(messages=[], response_delay_seconds=0.05, on_query=reject_once)
    with pytest.raises(TermApplicabilityRuntimeError) as caught:
        await ClaudeTermApplicabilityAgent(
            settings(term_applicability_timeout_seconds=0.01), client_factory=factory_for(client),
        ).execute(value, Tools())
    runtime = caught.value.runtime
    assert caught.value.code == "term_applicability_timeout"
    assert runtime.terminal_kind == "timeout"
    assert runtime.finalization_submission_count == 1  # the finalizer was reached
    assert runtime.finalization_accepted is False  # but never accepted
    assert runtime.correction_attempt_count == 1  # a correction was granted before the hang
    assert runtime.failure_stage == "evidence_identifier_unresolved"


@pytest.mark.asyncio
async def test_cancellation_raises_typed_error_with_partial_runtime_attached():
    """External cancellation is wrapped as a typed error carrying the partial runtime.

    A bare `raise` of `asyncio.CancelledError` (a `BaseException`, not an
    `Exception`) would skip past `TermApplicabilityHandler`'s
    `except Exception` clause entirely -- no `mark_run_failed` call would
    ever happen, silently discarding this exact partial-progress runtime and
    leaving the persisted run stuck at `status="running"` forever.
    """

    value = task()
    client = FakeClient(connect_error=asyncio.CancelledError())
    with pytest.raises(TermApplicabilityRuntimeError) as caught:
        await ClaudeTermApplicabilityAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert caught.value.code == "term_applicability_cancelled"
    assert caught.value.runtime is not None
    assert caught.value.runtime.terminal_kind == "cancelled"
    assert caught.value.runtime.terminal_reason == "cancelled"


@pytest.mark.asyncio
async def test_missing_finalizer_call_raises_validation_error():
    value = task()
    client = FakeClient(messages=[sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s")])
    with pytest.raises(TermApplicabilityRuntimeError) as caught:
        await ClaudeTermApplicabilityAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert caught.value.code == "term_applicability_finalizer_not_called"


@pytest.mark.asyncio
async def test_connect_transport_failure_is_safe_and_distinct():
    value = task()
    client = FakeClient(connect_error=RuntimeError("boom"))
    with pytest.raises(TermApplicabilityRuntimeError) as caught:
        await ClaudeTermApplicabilityAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert caught.value.code == "term_applicability_sdk_initialization_failed"
    assert "boom" not in repr(caught.value)


@pytest.mark.asyncio
async def test_mcp_status_failure_raises_initialization_error():
    value = task()
    client = FakeClient(status_error=RuntimeError("boom"))
    with pytest.raises(TermApplicabilityRuntimeError) as caught:
        await ClaudeTermApplicabilityAgent(settings(), client_factory=factory_for(client)).execute(value, Tools())
    assert caught.value.code == "term_applicability_mcp_initialization_failed"


# --- candidate commercial facts ---------------------------------------------------

@pytest.mark.asyncio
async def test_finalizer_accepts_a_grounded_candidate_commercial_fact():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=True)
    response = await adapters[-1].handler({"result": candidate_facts_payload(candidate_fact())})
    assert not response.get("is_error"), response
    result = runtime._finalization_capture.accepted_result
    fact = result.candidate_commercial_facts[0].facts[0]
    assert fact.raw_value == "10" and fact.field.value == "quantity"
    assert fact.fact_id  # server-computed, never supplied by the payload above


@pytest.mark.asyncio
async def test_finalizer_resolves_fact_evidence_ids_to_the_exact_trusted_reference():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=True)
    response = await adapters[-1].handler({"result": candidate_facts_payload(candidate_fact())})
    assert not response.get("is_error"), response
    fact = runtime._finalization_capture.accepted_result.candidate_commercial_facts[0].facts[0]
    assert fact.evidence == (value.evidence_by_id["native:p0001:b000001"],)


@pytest.mark.asyncio
async def test_finalizer_accepts_multiple_yearly_price_periods_for_one_candidate():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=True)
    facts = [
        candidate_fact(field="yearly_price", raw_value="$100", raw_period_label="Year 1"),
        candidate_fact(field="yearly_price", raw_value="$110", raw_period_label="Year 2"),
        candidate_fact(field="yearly_price", raw_value="$120", raw_period_label="Year 3"),
    ]
    response = await adapters[-1].handler({"result": candidate_facts_payload(*facts)})
    assert not response.get("is_error"), response
    result = runtime._finalization_capture.accepted_result
    assert len(result.candidate_commercial_facts[0].facts) == 3
    assert len({fact.fact_id for fact in result.candidate_commercial_facts[0].facts}) == 3
    assert runtime.candidate_commercial_fact_counts_by_field == {"yearly_price": 3}


@pytest.mark.asyncio
async def test_finalizer_rejects_facts_for_an_ineligible_candidate_status():
    value = task_with_excluded_candidate()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=False)
    runtime.record_candidate_evidence_retrieved("candidate-0002", "native:p0001:b000001")
    payload = candidate_facts_payload(candidate_fact(candidate_id="candidate-0002"), candidate_id="candidate-0002")
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"]
    assert "fact_candidate_not_eligible" in runtime.validation_issue_codes
    assert runtime.correction_attempt_count == 1


@pytest.mark.asyncio
async def test_finalizer_rejects_facts_for_an_unknown_candidate():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    payload = candidate_facts_payload(candidate_fact(candidate_id="candidate-9999"), candidate_id="candidate-9999")
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"]
    assert "fact_unknown_candidate" in runtime.validation_issue_codes
    assert runtime.correction_attempt_count == 0


@pytest.mark.asyncio
async def test_finalizer_rejects_an_unsupported_fact_field_name():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    payload = candidate_facts_payload(candidate_fact(field="sku_mapping"))
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"]
    assert runtime.failure_stage == "evidence_identifier_unresolved"


@pytest.mark.asyncio
async def test_finalizer_rejects_a_fact_citing_an_evidence_id_not_in_the_task():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    payload = candidate_facts_payload(candidate_fact(evidence_ids=("not-a-real-id",)))
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"]
    assert runtime.failure_stage == "evidence_identifier_unresolved"
    assert runtime.correction_attempt_count == 1


@pytest.mark.asyncio
async def test_finalizer_rejects_a_fact_never_grounded_by_a_successful_retrieval_this_run():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    response = await adapters[-1].handler({"result": candidate_facts_payload(candidate_fact())})
    assert response["is_error"]
    assert "fact_evidence_not_retrieved" in runtime.validation_issue_codes
    assert runtime.correction_attempt_count == 1


@pytest.mark.asyncio
async def test_finalizer_rejects_duplicate_same_field_facts_in_one_submission():
    """Two same-field, same-period facts collapse onto one canonical fact_id and are rejected."""

    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=True)
    payload = candidate_facts_payload(candidate_fact(raw_value="10"), candidate_fact(raw_value="20"))
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"]
    assert runtime.failure_stage == "pydantic_schema_validation"
    assert any(error["type"] == "duplicate_fact_id_in_bundle" for error in runtime.pydantic_errors)
    assert runtime.correction_attempt_count == 1


@pytest.mark.asyncio
async def test_finalizer_rejects_a_fact_shaped_item_missing_required_fact_fields():
    """An item shaped like a decision, not a fact, fails type checks before evidence resolution."""

    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    payload = {"candidate_commercial_facts": [{"candidate_id": "candidate-0001", "facts": [
        {"term_id": "term-0001", "disposition": "line_item", "applicability_scope": "document", "evidence_ids": ["native:p0001:b000001"]},
    ]}]}
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"]
    assert runtime.failure_stage == "evidence_identifier_unresolved"


@pytest.mark.asyncio
async def test_one_correction_max_applies_to_fact_submissions_too():
    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    finalize = adapters[-1]
    bad = candidate_facts_payload(candidate_fact(evidence_ids=("not-a-real-id",)))
    first = await finalize.handler({"result": bad})
    assert first["is_error"] and runtime.correction_attempt_count == 1
    runtime.record_candidate_evidence_retrieved("candidate-0001", "native:p0001:b000001")
    good = candidate_facts_payload(candidate_fact())
    second = await finalize.handler({"result": good})
    assert not second.get("is_error"), second
    third = await finalize.handler({"result": bad})
    assert third["is_error"]
    assert "correction_allowed\":false" in third["content"][0]["text"] or '"correction_allowed": false' in third["content"][0]["text"]


@pytest.mark.asyncio
async def test_decisions_and_candidate_commercial_facts_are_not_confused():
    """Term decisions and candidate facts submit together but land in structurally disjoint fields."""

    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=True, ground_candidate=True)
    payload = {**document_scope_payload(), **candidate_facts_payload(candidate_fact())}
    response = await adapters[-1].handler({"result": payload})
    assert not response.get("is_error"), response
    result = runtime._finalization_capture.accepted_result
    assert result.decisions[0].term_id == "term-0001"
    assert result.candidate_commercial_facts[0].facts[0].field.value == "quantity"


# --- candidate commercial fact coverage: application-computed extracted_fields ------

def test_finalizer_schema_excludes_extracted_fields_from_coverage():
    """`extracted_fields` cannot be part of a live submission -- it is not even in the schema."""

    schema = ClaudeTermApplicabilityAgent._finalizer_input_schema()
    coverage_definition = schema["$defs"]["CandidateCommercialFactCoverage"]
    assert "extracted_fields" not in coverage_definition["properties"]
    assert "extracted_fields" not in coverage_definition.get("required", [])


@pytest.mark.asyncio
async def test_coverage_extracted_fields_are_computed_from_submitted_facts():
    """Claude submits only unresolved_fields; the application derives extracted_fields from the facts.

    This is the exact fix for the real `of-0006` failure
    (`candidate_fact_coverage_extracted_mismatch`): a model-submitted
    `extracted_fields` could drift from its own `candidate_commercial_facts`.
    Since the field is no longer accepted from Claude at all, that
    particular mismatch is now structurally impossible.
    """

    value = task_with_expected_fact_fields()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=True)
    payload = {
        **candidate_facts_payload(candidate_fact(field="quantity", raw_value="5")),
        **coverage_payload(unresolved_fields=[
            "invoicing_frequency", "invoicing_schedule_type", "payment_terms",
            "service_end_date", "service_start_date", "unit_price",
        ]),
    }
    response = await adapters[-1].handler({"result": payload})
    assert not response.get("is_error"), response
    coverage = runtime._finalization_capture.accepted_result.candidate_commercial_fact_coverage[0]
    assert coverage.extracted_fields == (RawCommercialFactField.QUANTITY,)
    assert coverage.unresolved_fields == (
        RawCommercialFactField.INVOICING_FREQUENCY, RawCommercialFactField.INVOICING_SCHEDULE_TYPE,
        RawCommercialFactField.PAYMENT_TERMS, RawCommercialFactField.SERVICE_END_DATE,
        RawCommercialFactField.SERVICE_START_DATE, RawCommercialFactField.UNIT_PRICE,
    )
    assert set(coverage.expected_fields) == {
        RawCommercialFactField.QUANTITY, RawCommercialFactField.UNIT_PRICE,
        RawCommercialFactField.SERVICE_START_DATE, RawCommercialFactField.SERVICE_END_DATE,
        RawCommercialFactField.INVOICING_SCHEDULE_TYPE, RawCommercialFactField.INVOICING_FREQUENCY,
        RawCommercialFactField.PAYMENT_TERMS,
    }


@pytest.mark.asyncio
async def test_coverage_partitions_correctly_when_every_expected_field_is_extracted():
    """When every expected field has a fact, unresolved_fields is correctly empty."""

    value = task_with_expected_fact_fields()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=True)
    payload = {
        **candidate_facts_payload(
            candidate_fact(field="quantity", raw_value="5"),
            candidate_fact(field="unit_price", raw_value="$10.00"),
            candidate_fact(field="service_start_date", raw_value="2026-01-01"),
            candidate_fact(field="service_end_date", raw_value="2026-12-31"),
            candidate_fact(field="invoicing_schedule_type", raw_value="Recurring"),
            candidate_fact(field="invoicing_frequency", raw_value="Monthly"),
            candidate_fact(field="payment_terms", raw_value="Net 30"),
        ),
        **coverage_payload(unresolved_fields=[]),
    }
    response = await adapters[-1].handler({"result": payload})
    assert not response.get("is_error"), response
    coverage = runtime._finalization_capture.accepted_result.candidate_commercial_fact_coverage[0]
    assert set(coverage.extracted_fields) == {
        RawCommercialFactField.QUANTITY, RawCommercialFactField.UNIT_PRICE,
        RawCommercialFactField.SERVICE_START_DATE, RawCommercialFactField.SERVICE_END_DATE,
        RawCommercialFactField.INVOICING_SCHEDULE_TYPE, RawCommercialFactField.INVOICING_FREQUENCY,
        RawCommercialFactField.PAYMENT_TERMS,
    }
    assert coverage.unresolved_fields == ()


@pytest.mark.asyncio
async def test_coverage_accepts_legitimate_extra_facts_outside_the_hinted_expected_fields():
    """A field the model correctly extracted but that was never *expected* must not break coverage.

    Reproduces the real `of-0001` failure: `expected_fields` covers the
    raw-attribute-hinted core fields (quantity, unit_price) plus the
    always-required contract-item attributes once hinted, but Claude is
    free -- and expected -- to also extract facts entirely outside that set
    (currency, unit price period, ...). Before this fix, `extracted_fields`
    was computed from *every* submitted fact, so any such legitimate extra
    fact made `extracted_fields` a superset of `expected_fields` and the
    coverage partition invariant failed deterministically on every
    well-extracted document, not just hard-to-parse ones.
    """

    value = task_with_expected_fact_fields()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=True)
    payload = {
        **candidate_facts_payload(
            candidate_fact(field="quantity", raw_value="5"),
            candidate_fact(field="unit_price", raw_value="$10.00"),
            candidate_fact(field="service_start_date", raw_value="2026-01-01"),
            candidate_fact(field="service_end_date", raw_value="2026-12-31"),
            candidate_fact(field="invoicing_schedule_type", raw_value="Recurring"),
            candidate_fact(field="invoicing_frequency", raw_value="Monthly"),
            candidate_fact(field="payment_terms", raw_value="Net 30"),
            candidate_fact(field="currency", raw_value="USD"),  # extra, never expected for this candidate
        ),
        **coverage_payload(unresolved_fields=[]),
    }
    response = await adapters[-1].handler({"result": payload})
    assert not response.get("is_error"), response
    result = runtime._finalization_capture.accepted_result
    coverage = result.candidate_commercial_fact_coverage[0]
    assert set(coverage.extracted_fields) == {
        RawCommercialFactField.QUANTITY, RawCommercialFactField.UNIT_PRICE,
        RawCommercialFactField.SERVICE_START_DATE, RawCommercialFactField.SERVICE_END_DATE,
        RawCommercialFactField.INVOICING_SCHEDULE_TYPE, RawCommercialFactField.INVOICING_FREQUENCY,
        RawCommercialFactField.PAYMENT_TERMS,
    }
    assert coverage.unresolved_fields == ()
    facts = {fact.field for fact in result.candidate_commercial_facts[0].facts}
    assert facts == {
        RawCommercialFactField.QUANTITY, RawCommercialFactField.UNIT_PRICE,
        RawCommercialFactField.SERVICE_START_DATE, RawCommercialFactField.SERVICE_END_DATE,
        RawCommercialFactField.INVOICING_SCHEDULE_TYPE, RawCommercialFactField.INVOICING_FREQUENCY,
        RawCommercialFactField.PAYMENT_TERMS, RawCommercialFactField.CURRENCY,
    }


@pytest.mark.asyncio
async def test_finalizer_rejects_a_coverage_item_that_still_submits_extracted_fields():
    """Even if Claude tries to restate it anyway, the submission is rejected, not silently accepted."""

    value = task_with_expected_fact_fields()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=True)
    payload = {
        **candidate_facts_payload(candidate_fact(field="quantity", raw_value="5")),
        **coverage_payload(unresolved_fields=["unit_price"], extracted_fields=["quantity"]),
    }
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"]
    assert runtime.failure_stage == "evidence_identifier_unresolved"


@pytest.mark.asyncio
async def test_finalizer_rejects_overlap_between_computed_extracted_and_submitted_unresolved():
    """A field the facts show was actually extracted cannot also be declared unresolved."""

    value = task_with_expected_fact_fields()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=True)
    payload = {
        **candidate_facts_payload(candidate_fact(field="quantity", raw_value="5")),
        **coverage_payload(unresolved_fields=["quantity", "unit_price"]),
    }
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"]
    assert runtime.failure_stage == "pydantic_schema_validation"
    assert runtime.correction_attempt_count == 1


@pytest.mark.asyncio
async def test_finalizer_rejects_a_coverage_missing_an_expected_field_entirely():
    """A field that is neither extracted nor declared unresolved leaves expected_fields unaccounted."""

    value = task_with_expected_fact_fields()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=True)
    payload = {
        **candidate_facts_payload(candidate_fact(field="quantity", raw_value="5")),
        **coverage_payload(unresolved_fields=[]),  # unit_price is neither extracted nor unresolved
    }
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"]
    assert runtime.failure_stage == "pydantic_schema_validation"


@pytest.mark.asyncio
async def test_coverage_partition_mismatch_names_the_missing_field_in_correction_feedback():
    """A field left out of unresolved_fields must be named, not just a bare schema error code.

    `expected_fields`/`extracted_fields` are both server-computed, so a bare
    `value_error` at `candidate_commercial_fact_coverage.0` gives Claude no
    way to know which of its own `unresolved_fields` entries is missing.
    """

    value = task_with_expected_fact_fields()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=True)
    payload = {
        **candidate_facts_payload(candidate_fact(field="quantity", raw_value="5")),
        **coverage_payload(unresolved_fields=[]),  # every other expected field is unaccounted
    }
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"]
    issues = json.loads(response["content"][0]["text"])["issues"]
    assert len(issues) == 1
    assert issues[0]["code"] == "coverage_fields_do_not_partition_expected_fields"
    assert issues[0]["candidate_id"] == "candidate-0001"
    assert issues[0]["missing_from_both_extracted_and_unresolved"] == [
        "invoicing_frequency", "invoicing_schedule_type", "payment_terms",
        "service_end_date", "service_start_date", "unit_price",
    ]
    assert issues[0]["present_in_both_extracted_and_unresolved"] == []


@pytest.mark.asyncio
async def test_coverage_partition_overlap_names_the_double_counted_field_in_correction_feedback():
    """A field claimed as both extracted (via facts) and unresolved must be named as overlapping."""

    value = task_with_expected_fact_fields()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=True)
    payload = {
        **candidate_facts_payload(candidate_fact(field="quantity", raw_value="5")),
        **coverage_payload(unresolved_fields=[
            "invoicing_frequency", "invoicing_schedule_type", "payment_terms",
            "quantity", "service_end_date", "service_start_date", "unit_price",
        ]),
    }
    response = await adapters[-1].handler({"result": payload})
    assert response["is_error"]
    issues = json.loads(response["content"][0]["text"])["issues"]
    assert len(issues) == 1
    assert issues[0]["code"] == "coverage_fields_do_not_partition_expected_fields"
    assert issues[0]["present_in_both_extracted_and_unresolved"] == ["quantity"]
    assert issues[0]["missing_from_both_extracted_and_unresolved"] == []


@pytest.mark.asyncio
async def test_coverage_partition_diagnostics_reset_between_correction_attempts():
    """A clean second submission must not carry over a stale first-attempt diagnosis."""

    value = task_with_expected_fact_fields()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    ground_runtime(type("Options", (), {"_maximor_runtime": runtime})(), ground_term=False, ground_candidate=True)
    bad_payload = {
        **candidate_facts_payload(candidate_fact(field="quantity", raw_value="5")),
        **coverage_payload(unresolved_fields=[]),
    }
    first = await adapters[-1].handler({"result": bad_payload})
    assert first["is_error"]
    assert runtime.coverage_partition_issues

    good_payload = {
        **candidate_facts_payload(
            candidate_fact(field="quantity", raw_value="5"),
            candidate_fact(field="unit_price", raw_value="$10.00"),
            candidate_fact(field="service_start_date", raw_value="2026-01-01"),
            candidate_fact(field="service_end_date", raw_value="2026-12-31"),
            candidate_fact(field="invoicing_schedule_type", raw_value="Recurring"),
            candidate_fact(field="invoicing_frequency", raw_value="Monthly"),
            candidate_fact(field="payment_terms", raw_value="Net 30"),
        ),
        **coverage_payload(unresolved_fields=[]),
    }
    second = await adapters[-1].handler({"result": good_payload})
    assert not second.get("is_error"), second
    assert runtime.coverage_partition_issues == ()


def test_persistence_diagnostics_never_carry_raw_text_or_secrets():
    """The bounded diagnostics snapshot excludes prompts, document text, and credentials."""

    value = task()
    adapters, runtime = ClaudeTermApplicabilityAgent(settings())._build_adapters(value, Tools())
    diagnostics = runtime.persistence_diagnostics()
    blob = repr(diagnostics).lower()
    for forbidden in ("secret-test-key", "payment terms", "premium support", "select ", "bearer "):
        assert forbidden not in blob
