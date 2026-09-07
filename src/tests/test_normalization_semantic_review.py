"""Generated-fixture tests for bounded Normalization Stage 5B review."""

import uuid

import pytest
from pydantic import SecretStr, ValidationError

from maximor.config import DatabaseSettings
from maximor.normalization.finalization import assess_finalization_readiness
from maximor.normalization.schemas import FinalizationIssue, FinalizationReadiness, FinalizationStatus, ReviewQueueClassification
from maximor.normalization.semantic_review.agent import ClaudeNormalizationSemanticReviewAgent, SemanticReviewRuntimeSummary
from maximor.normalization.semantic_review.contracts import build_semantic_review_task, review_item_id
from maximor.normalization.semantic_review.errors import SemanticReviewTaskError, SemanticReviewValidationError
from maximor.normalization.semantic_review.schemas import NormalizationSemanticReviewResult, SemanticReviewOutcome
from maximor.normalization.semantic_review.tools import NormalizationSemanticReviewTools
from maximor.normalization.service import assemble_normalized_draft
from test_normalization_assembly import _assemble


def review_ready_input():
    normalization_input = _assemble()
    draft = assemble_normalized_draft(normalization_input)
    issue = FinalizationIssue(
        code="ambiguous_value", classification=ReviewQueueClassification.AMBIGUOUS_VALUE,
        message="An assigned value needs bounded interpretation.", location="line_items[candidate-hinted].payment_terms",
        candidate_id="candidate-hinted", field_name="payment_terms",
    )
    readiness = FinalizationReadiness(policy_version="0.1.0", status=FinalizationStatus.REVIEW_REQUIRED, issues=(issue,))
    return normalization_input, draft, readiness


def test_task_excludes_deterministic_only_and_failed_issues():
    normalization_input, draft, _ = review_ready_input()
    failed = FinalizationReadiness(
        policy_version="0.1.0", status=FinalizationStatus.FAILED_VALIDATION,
        issues=(FinalizationIssue(code="bad", classification=ReviewQueueClassification.HARD_INVARIANT, message="bad", location="identity", hard=True),),
    )
    with pytest.raises(SemanticReviewTaskError):
        build_semantic_review_task(normalization_input, draft, failed)
    only_missing = FinalizationReadiness(
        policy_version="0.1.0", status=FinalizationStatus.REVIEW_REQUIRED,
        issues=(FinalizationIssue(code="missing", classification=ReviewQueueClassification.MISSING_VALUE, message="missing", location="line_items[x].quantity"),),
    )
    with pytest.raises(SemanticReviewTaskError):
        build_semantic_review_task(normalization_input, draft, only_missing)


def test_task_is_bounded_and_request_bound_without_sensitive_payloads():
    normalization_input, draft, readiness = review_ready_input()
    task = build_semantic_review_task(normalization_input, draft, readiness)
    assert task.request.organization_id == normalization_input.organization_id
    assert task.items[0].candidate_id == "candidate-hinted"
    serialized = str(task.model_dump(mode="json"))
    assert "/Users/" not in serialized and "SELECT " not in serialized.upper()
    assert "raw_value" not in serialized


class Resolver:
    async def resolve(self, evidence_id):
        return {"safe": evidence_id}


@pytest.mark.asyncio
async def test_tools_require_assigned_scope_and_record_successful_evidence_only():
    normalization_input, draft, readiness = review_ready_input()
    task = build_semantic_review_task(normalization_input, draft, readiness)
    runtime = SemanticReviewRuntimeSummary()
    tools = NormalizationSemanticReviewTools(task, Resolver(), runtime)
    await tools.get_review_item_context(task.items[0].review_item_id)
    evidence_id = task.items[0].allowed_evidence_ids[0]
    await tools.get_review_item_evidence(task.items[0].review_item_id, evidence_id)
    assert runtime.successful_tool_call_order == ("get_review_item_context", "get_review_item_context", "get_review_item_evidence")
    with pytest.raises(Exception):
        await tools.get_review_item_evidence(task.items[0].review_item_id, "foreign-evidence")


def test_server_finalizer_accepts_supported_finding_only_after_retrieval():
    normalization_input, draft, readiness = review_ready_input()
    task = build_semantic_review_task(normalization_input, draft, readiness)
    item = task.items[0]
    runtime = SemanticReviewRuntimeSummary()
    runtime.record_successful_tool("get_review_item_evidence", evidence_id=item.allowed_evidence_ids[0])
    payload = {"findings": [{"review_item_id": item.review_item_id, "outcome": "supported_interpretation", "candidate_id": item.candidate_id, "field_name": item.field_name, "evidence_ids": [item.allowed_evidence_ids[0]], "rationale": "Evidence supports a bounded interpretation."}]}
    result = ClaudeNormalizationSemanticReviewAgent.validate_submission(task, payload, runtime)
    assert result.findings[0].outcome is SemanticReviewOutcome.SUPPORTED_INTERPRETATION


def test_insufficient_evidence_does_not_claim_support_and_foreign_evidence_rejected():
    normalization_input, draft, readiness = review_ready_input()
    task = build_semantic_review_task(normalization_input, draft, readiness)
    item = task.items[0]
    runtime = SemanticReviewRuntimeSummary()
    insufficient = {"findings": [{"review_item_id": item.review_item_id, "outcome": "insufficient_evidence", "candidate_id": item.candidate_id, "field_name": item.field_name}]}
    assert ClaudeNormalizationSemanticReviewAgent.validate_submission(task, insufficient, runtime).findings[0].outcome is SemanticReviewOutcome.INSUFFICIENT_EVIDENCE
    foreign = {"findings": [{"review_item_id": item.review_item_id, "outcome": "supported_interpretation", "candidate_id": item.candidate_id, "field_name": item.field_name, "evidence_ids": ["foreign"]}]}
    with pytest.raises(SemanticReviewValidationError):
        ClaudeNormalizationSemanticReviewAgent.validate_submission(task, foreign, runtime)


def test_schema_invalid_submission_records_specific_pydantic_error_detail():
    """A bare `semantic_review_schema_invalid` code left the model's one correction with nothing to act on.

    Reproduces the real `OF-0008` failure: a schema-invalid submission (here,
    an `outcome` value outside the fixed enum) must record a bounded,
    specific location/type on `runtime.pydantic_errors` -- not just the
    generic code -- so the correction feedback can actually name the field.
    """

    normalization_input, draft, readiness = review_ready_input()
    task = build_semantic_review_task(normalization_input, draft, readiness)
    item = task.items[0]
    runtime = SemanticReviewRuntimeSummary()
    bad_outcome = {"findings": [{"review_item_id": item.review_item_id, "outcome": "not_a_real_outcome", "candidate_id": item.candidate_id, "field_name": item.field_name}]}
    with pytest.raises(SemanticReviewValidationError):
        ClaudeNormalizationSemanticReviewAgent.validate_submission(task, bad_outcome, runtime)
    assert runtime.failure_stage == "pydantic_schema_validation"
    assert len(runtime.pydantic_errors) == 1
    assert runtime.pydantic_errors[0]["location"] == "findings.0.outcome"
    assert runtime.pydantic_errors[0]["type"]

    good = {"findings": [{"review_item_id": item.review_item_id, "outcome": "insufficient_evidence", "candidate_id": item.candidate_id, "field_name": item.field_name}]}
    ClaudeNormalizationSemanticReviewAgent.validate_submission(task, good, runtime)
    assert runtime.pydantic_errors == ()


def test_findings_ordering_violation_maps_to_a_specific_safe_code():
    """The model-level ordering/uniqueness validator must not collapse to a bare code.

    Reproduces the real `OF-0008`/`OF-0016` failure: after the schema fix
    stopped the model from guessing field names, it started failing a
    *different*, model-level check instead -- `findings` not submitted in
    ascending order by `review_item_id`, or containing a duplicate. That
    validator raises a bare `ValueError` at the model root (pydantic gives
    it no field-specific `loc`), so without this mapping the correction
    feedback said nothing more than a generic pydantic error type.
    """

    error = {"type": "value_error", "msg": "Value error, findings must be unique and ordered by review_item_id"}
    assert ClaudeNormalizationSemanticReviewAgent._map_pydantic_error(error) == "findings_not_unique_or_ordered_by_review_item_id"


def test_finalizer_schema_declares_real_field_names_and_enum_values():
    """The finalizer's own JSON schema must give Claude the real shape, not a bare object.

    Reproduces the real `OF-0008` failure: with a bare `{"type": "object"}"`
    for `result`, the model had zero structural guidance and reliably
    guessed at the payload shape (observed: submitting `verdict` where the
    schema requires `outcome`). Deriving the schema from
    `NormalizationSemanticReviewResult` itself must surface the actual
    field name and the real `SemanticReviewOutcome`/`SemanticReviewOwner`
    enum values, and must never re-offer the trusted identity fields
    `validate_submission` always injects itself.
    """

    schema = ClaudeNormalizationSemanticReviewAgent._finalizer_input_schema()
    result_schema = schema["properties"]["result"]
    assert result_schema["required"] == ["findings"]
    for trusted_field in ("schema_version", "organization_id", "document_id", "agent_version"):
        assert trusted_field not in result_schema["properties"]

    finding_definition = schema["$defs"]["SemanticReviewFinding"]
    assert "outcome" in finding_definition["properties"]
    assert "verdict" not in finding_definition["properties"]
    assert finding_definition["required"] == ["review_item_id", "outcome"]
    outcome_enum = schema["$defs"]["SemanticReviewOutcome"]["enum"]
    assert set(outcome_enum) == {
        "supported_interpretation", "insufficient_evidence", "conflict_unresolved", "route_for_targeted_correction",
    }


def test_missing_duplicate_or_out_of_order_findings_rejected():
    normalization_input, draft, readiness = review_ready_input()
    task = build_semantic_review_task(normalization_input, draft, readiness)
    runtime = SemanticReviewRuntimeSummary()
    with pytest.raises(SemanticReviewValidationError):
        ClaudeNormalizationSemanticReviewAgent.validate_submission(task, {"findings": []}, runtime)
    finding = {"review_item_id": task.items[0].review_item_id, "outcome": "insufficient_evidence"}
    with pytest.raises(ValidationError):
        NormalizationSemanticReviewResult(
            schema_version="0.1.0", organization_id=task.request.organization_id, document_id=task.request.document_id,
            preprocessing_run_id=task.request.preprocessing_run_id, analysis_run_id=task.request.analysis_run_id,
            normalization_schema_version=task.request.normalization_schema_version,
            finalization_policy_version=task.request.finalization_policy_version,
            prompt_version=task.request.prompt_version, skill_version=task.request.skill_version,
            agent_version=task.request.agent_version,
            findings=(finding, finding),
        )


def test_exact_namespaced_allowlist_and_prompt_are_isolated():
    normalization_input, draft, readiness = review_ready_input()
    task = build_semantic_review_task(normalization_input, draft, readiness)
    settings = DatabaseSettings(database_url=SecretStr("postgresql+asyncpg://test:test@localhost/test"), anthropic_api_key=SecretStr("test-key"))
    agent = ClaudeNormalizationSemanticReviewAgent(settings)
    options = agent.build_options(task, NormalizationSemanticReviewTools(task))
    assert tuple(options.allowed_tools) == (
        "mcp__maximor_normalization_semantic_review__get_review_item_context",
        "mcp__maximor_normalization_semantic_review__get_review_item_evidence",
        "mcp__maximor_normalization_semantic_review__get_candidate_context",
        "mcp__maximor_normalization_semantic_review__get_term_context",
        "mcp__maximor_normalization_semantic_review__finalize_normalization_semantic_review",
    )
    assert "get_review_item_context" in agent._prompt(task)
    assert "finalize_normalization_semantic_review" in agent._prompt(task)


@pytest.mark.asyncio
async def test_execute_stops_once_the_one_allowed_correction_is_also_rejected():
    """The session must not keep burning turns on a hopeless finalizer.

    Reproduces the real `OF-0008` failure: the model kept calling `finalize`
    (10 times) long after its one allowed correction had already been
    rejected, because the receive-response loop only ever checked for an
    *accepted* result, never for an exhausted-and-rejected one. It must now
    break as soon as `capture["rejected"]` is set, and `execute()` must
    raise the accurate validation failure rather than claim the finalizer
    was never called.
    """

    normalization_input, draft, readiness = review_ready_input()
    task = build_semantic_review_task(normalization_input, draft, readiness)
    settings = DatabaseSettings(database_url=SecretStr("postgresql+asyncpg://test:test@localhost/test"), anthropic_api_key=SecretStr("test-key"))
    agent = ClaudeNormalizationSemanticReviewAgent(settings)
    processed: list[int] = []

    class _DummyMessage:
        pass

    class FakeClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            pass

        async def query(self, prompt):
            finalize = self.options._maximor_adapters[-1].handler
            await finalize({"result": {}})  # consumes the one allowed correction
            await finalize({"result": {}})  # exhausts it -- capture["rejected"] = True

        async def receive_response(self):
            for _ in range(5):
                processed.append(1)
                yield _DummyMessage()

        async def disconnect(self):
            pass

    agent._client_factory = lambda options: FakeClient(options)
    tools = NormalizationSemanticReviewTools(task)
    with pytest.raises(SemanticReviewValidationError):
        await agent.execute(task, tools)
    assert len(processed) == 1


def test_runtime_diagnostics_are_bounded_and_do_not_mutate_source():
    normalization_input, draft, readiness = review_ready_input()
    before = normalization_input.model_dump(mode="json")
    runtime = SemanticReviewRuntimeSummary()
    runtime.record_successful_tool("get_review_item_evidence", evidence_id="safe-id")
    summary = runtime.safe_summary()
    assert summary["retrieved_evidence_ids"] == ["safe-id"]
    assert normalization_input.model_dump(mode="json") == before
    assert "prompt" not in str(summary).lower()
