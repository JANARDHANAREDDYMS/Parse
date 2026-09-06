"""Prove DocumentAnalysisAgent's term-applicability decoupling end to end.

Non-paid: exercises only the pure Pydantic draft/promotion contracts and the
agent's own local validation/schema methods directly. No Claude call, no
documents, no database.
"""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from maximor.document_analysis.agent import ClaudeDocumentAnalysisAgent, DocumentAnalysisRuntimeSummary
from maximor.document_analysis.contracts import DocumentAnalysisRequest
from maximor.document_analysis.draft import DraftDocumentAnalysisResult, DraftGlobalTerm, promote_draft_result
from maximor.document_analysis.schemas import ApplicabilityScope, DocumentAnalysisResult, GlobalTerm


def _request() -> DocumentAnalysisRequest:
    return DocumentAnalysisRequest(
        organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=uuid.uuid4(), preprocessing_schema_version="1",
        document_analysis_schema_version="1", prompt_version="p1",
        skill_version="skill-1", agent_version="a1",
    )


def _evidence(preprocessing_run_id: uuid.UUID) -> dict:
    return {
        "preprocessing_run_id": str(preprocessing_run_id), "page_number": 1,
        "block_id": "native:p0001:b000000", "representation": "native_text",
        "extraction_source": "native",
    }


def _submission(value: DocumentAnalysisRequest, *, term_extra: dict | None = None) -> dict:
    """Return a minimal finalizer submission with one draft-shaped global term."""

    evidence = _evidence(value.preprocessing_run_id)
    term = {"term_id": "term:0001", "raw_name": "Billing frequency", "raw_value": "Monthly", "evidence": [evidence]}
    if term_extra:
        term.update(term_extra)
    return {
        "schema_version": value.document_analysis_schema_version,
        "organization_id": str(value.organization_id), "document_id": str(value.document_id),
        "preprocessing_run_id": str(value.preprocessing_run_id),
        "preprocessing_schema_version": value.preprocessing_schema_version,
        "prompt_version": value.prompt_version, "agent_version": value.agent_version,
        "global_terms": [term],
        "evidence_references": [evidence],
    }


def _runtime() -> DocumentAnalysisRuntimeSummary:
    """Return a runtime pre-grounded with the mandatory overview/content-retrieval facts.

    The completion gate (unrelated to term applicability) requires evidence
    that get_document_overview and at least one content-retrieval tool were
    called before it accepts any finalizer submission.
    """

    runtime = DocumentAnalysisRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC))
    runtime.tool_call_count = 2
    runtime.tool_calls_by_name = {"get_document_overview": 1, "get_page_text": 1}
    runtime.successful_tool_call_order = ("get_document_overview", "get_page_text")
    runtime.referenced_pages = (1,)
    return runtime


# --- Finalizer schema excludes applicability -----------------------------------

def test_finalizer_schema_excludes_applicability_fields_anywhere():
    """No applicability field name appears anywhere in the LLM-facing schema."""

    schema = ClaudeDocumentAnalysisAgent._finalizer_input_schema()
    import json
    dumped = json.dumps(schema)
    assert "applicability_scope" not in dumped
    assert "applies_to_candidate_ids" not in dumped


def test_draft_global_term_forbids_applicability_fields():
    """DraftGlobalTerm has no applicability fields to fill in, and forbids extras."""

    with pytest.raises(ValidationError):
        DraftGlobalTerm(term_id="term:0001", raw_name="Limit", applicability_scope="candidate")
    with pytest.raises(ValidationError):
        DraftGlobalTerm(term_id="term:0001", raw_name="Limit", applies_to_candidate_ids=["candidate-0001"])
    draft = DraftGlobalTerm(term_id="term:0001", raw_name="Limit", raw_value="10")
    assert not hasattr(draft, "applicability_scope")
    assert not hasattr(draft, "applies_to_candidate_ids")


# --- Accepted draft persists canonical unknown/empty applicability -------------

def test_accepted_draft_promotes_every_term_to_unknown_and_empty_candidates():
    """A clean draft submission yields canonical terms with server-assigned unknown scope."""

    value = _request()
    result, correctable = ClaudeDocumentAnalysisAgent(_settings())._validate_structured_output(
        value, _submission(value), _runtime(),
    )
    assert result is not None and correctable is False
    assert len(result.global_terms) == 1
    term = result.global_terms[0]
    assert term.applicability_scope == ApplicabilityScope.UNKNOWN
    assert term.applies_to_candidate_ids == ()
    assert term.raw_name == "Billing frequency" and term.raw_value == "Monthly"


def test_promote_draft_result_is_the_only_conversion_path():
    """promote_draft_result deterministically assigns unknown/empty, independent of the agent."""

    value = _request()
    draft = DraftDocumentAnalysisResult.model_validate(_submission(value))
    result = promote_draft_result(draft)
    assert isinstance(result, DocumentAnalysisResult)
    assert result.global_terms[0].applicability_scope == ApplicabilityScope.UNKNOWN
    assert result.global_terms[0].applies_to_candidate_ids == ()


# --- The LLM cannot override the server-side default ---------------------------

def test_llm_cannot_override_applicability_via_extra_field():
    """A term dict that adds applicability fields is rejected, not silently honored."""

    value = _request()
    result, correctable = ClaudeDocumentAnalysisAgent(_settings())._validate_structured_output(
        value,
        _submission(value, term_extra={"applicability_scope": "candidate", "applies_to_candidate_ids": ["candidate-0001"]}),
        (runtime := _runtime()),
    )
    assert result is None
    assert runtime.failure_stage == "pydantic_schema_validation"
    assert runtime.pydantic_errors


# --- Legacy artifact loading remains valid --------------------------------------

def test_legacy_artifact_without_applicability_fields_still_loads():
    """A pre-applicability persisted artifact still deserializes with unknown defaults."""

    value = _request()
    evidence = _evidence(value.preprocessing_run_id)
    legacy_json = (
        '{"schema_version": "1", "organization_id": "%s", "document_id": "%s", '
        '"preprocessing_run_id": "%s", "preprocessing_schema_version": "1", '
        '"prompt_version": "p1", "agent_version": "a1", '
        '"global_terms": [{"term_id": "term:legacy", "raw_name": "Payment"}], '
        '"evidence_references": []}'
    ) % (value.organization_id, value.document_id, value.preprocessing_run_id)
    result = DocumentAnalysisResult.model_validate_json(legacy_json)
    assert result.global_terms[0].applicability_scope == ApplicabilityScope.UNKNOWN
    assert result.global_terms[0].applies_to_candidate_ids == ()


def _settings():
    from maximor.config import DatabaseSettings
    return DatabaseSettings(
        database_url="postgresql+asyncpg://test", anthropic_api_key="secret-test-key",
        document_analysis_project_root="..",
    )
