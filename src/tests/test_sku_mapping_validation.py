"""Test the pure SKU-mapping decision validation gate: no database, no Claude call."""

import uuid
from dataclasses import dataclass, field

import pytest

from maximor.document_analysis.schemas import CommercialStatus, EvidenceReference, EvidenceRepresentation, ExtractionSource
from maximor.sku_mapping.contracts import SkuMappingTask
from maximor.sku_mapping.schemas import SkuMappingDecision, SkuMappingOutcome, SkuRecord
from maximor.sku_mapping.validation import validate_sku_mapping_decision
from maximor.sku_mapping.versions import SKU_MAPPING_DECISION_SCHEMA_VERSION, SKU_MAPPING_TASK_SCHEMA_VERSION


def _evidence(preprocessing_run_id: uuid.UUID, block_id: str = "native:p0001:b000000") -> EvidenceReference:
    return EvidenceReference(
        preprocessing_run_id=preprocessing_run_id, page_number=1, block_id=block_id,
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    )


def _task() -> SkuMappingTask:
    preprocessing_run_id = uuid.uuid4()
    return SkuMappingTask(
        schema_version=SKU_MAPPING_TASK_SCHEMA_VERSION, organization_id=uuid.uuid4(), document_id=uuid.uuid4(),
        preprocessing_run_id=preprocessing_run_id, analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="a", document_analysis_agent_version="a",
        candidate_id="candidate-0001", raw_name="Premium Support",
        commercial_status=CommercialStatus.PURCHASED, candidate_evidence=(_evidence(preprocessing_run_id),),
    )


def _sku(task: SkuMappingTask, catalog_version_id: uuid.UUID, *, sku_code="PREMIUM_SUPPORT", name="Premium Support", is_active=True, organization_id=None) -> SkuRecord:
    return SkuRecord(
        id=uuid.uuid4(), source_sku_id=uuid.uuid4(), organization_id=organization_id or task.organization_id,
        catalog_version_id=catalog_version_id, sku_code=sku_code, name=name, is_active=is_active,
    )


def _match_decision(task: SkuMappingTask, sku: SkuRecord, catalog_version_id: uuid.UUID, **overrides) -> SkuMappingDecision:
    kwargs = dict(
        schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION, organization_id=task.organization_id,
        document_id=task.document_id, preprocessing_run_id=task.preprocessing_run_id,
        analysis_run_id=task.analysis_run_id, candidate_id=task.candidate_id, catalog_version_id=catalog_version_id,
        outcome=SkuMappingOutcome.MATCH, sku_id=sku.id, sku_code=sku.sku_code, sku_name=sku.name,
        evidence=task.candidate_evidence,
    )
    kwargs.update(overrides)
    return SkuMappingDecision(**kwargs)


@dataclass
class _Runtime:
    tool_calls_by_name: dict = field(default_factory=dict)
    authoritative_skus_by_id: dict = field(default_factory=dict)


def test_valid_match_decision_with_full_runtime_has_no_issues():
    """Accept a MATCH decision that retrieved, confirmed, and agrees with the confirmed record."""

    task = _task()
    catalog_version_id = uuid.uuid4()
    sku = _sku(task, catalog_version_id)
    decision = _match_decision(task, sku, catalog_version_id)
    runtime = _Runtime(tool_calls_by_name={"retrieve_skus": 1, "get_authoritative_sku": 1}, authoritative_skus_by_id={sku.id: sku})

    assert validate_sku_mapping_decision(task, decision, runtime) == ()


def test_no_match_decision_only_needs_retrieval():
    """Accept a NO_MATCH decision once retrieval ran, with no authoritative-lookup requirement."""

    task = _task()
    decision = SkuMappingDecision(
        schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION, organization_id=task.organization_id,
        document_id=task.document_id, preprocessing_run_id=task.preprocessing_run_id,
        analysis_run_id=task.analysis_run_id, candidate_id=task.candidate_id, catalog_version_id=uuid.uuid4(),
        outcome=SkuMappingOutcome.NO_MATCH, evidence=task.candidate_evidence,
    )
    runtime = _Runtime(tool_calls_by_name={"retrieve_skus": 1})

    assert validate_sku_mapping_decision(task, decision, runtime) == ()


def test_identity_mismatch_is_detected():
    """Reject a decision whose candidate_id does not match the trusted task."""

    task = _task()
    catalog_version_id = uuid.uuid4()
    sku = _sku(task, catalog_version_id)
    decision = _match_decision(task, sku, catalog_version_id, candidate_id="candidate-9999")
    runtime = _Runtime(tool_calls_by_name={"retrieve_skus": 1, "get_authoritative_sku": 1}, authoritative_skus_by_id={sku.id: sku})

    issues = validate_sku_mapping_decision(task, decision, runtime)
    assert any(issue.code == "identity_mismatch" and not issue.correctable for issue in issues)


def test_version_mismatch_is_detected():
    """Reject a decision claiming a schema version other than the expected one."""

    task = _task()
    catalog_version_id = uuid.uuid4()
    sku = _sku(task, catalog_version_id)
    decision = _match_decision(task, sku, catalog_version_id, schema_version="0.0.1")
    runtime = _Runtime(tool_calls_by_name={"retrieve_skus": 1, "get_authoritative_sku": 1}, authoritative_skus_by_id={sku.id: sku})

    issues = validate_sku_mapping_decision(task, decision, runtime)
    assert any(issue.code == "version_mismatch" for issue in issues)


def test_missing_retrieval_is_detected_even_with_no_runtime():
    """Treat a missing runtime the same as zero successful tool calls."""

    task = _task()
    decision = SkuMappingDecision(
        schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION, organization_id=task.organization_id,
        document_id=task.document_id, preprocessing_run_id=task.preprocessing_run_id,
        analysis_run_id=task.analysis_run_id, candidate_id=task.candidate_id, catalog_version_id=uuid.uuid4(),
        outcome=SkuMappingOutcome.NO_MATCH, evidence=task.candidate_evidence,
    )

    issues = validate_sku_mapping_decision(task, decision, None)
    assert any(issue.code == "retrieval_not_performed" and not issue.correctable for issue in issues)


def test_evidence_not_from_task_is_detected_and_correctable():
    """Reject decision evidence that was not part of the task's own evidence, but allow a fix."""

    task = _task()
    foreign_evidence = _evidence(task.preprocessing_run_id, block_id="native:p0001:b999999")
    decision = SkuMappingDecision(
        schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION, organization_id=task.organization_id,
        document_id=task.document_id, preprocessing_run_id=task.preprocessing_run_id,
        analysis_run_id=task.analysis_run_id, candidate_id=task.candidate_id, catalog_version_id=uuid.uuid4(),
        outcome=SkuMappingOutcome.NO_MATCH, evidence=(foreign_evidence,),
    )
    runtime = _Runtime(tool_calls_by_name={"retrieve_skus": 1})

    issues = validate_sku_mapping_decision(task, decision, runtime)
    assert any(issue.code == "evidence_not_grounded_in_task" and issue.correctable for issue in issues)


def test_match_without_authoritative_lookup_call_is_detected():
    """Reject a MATCH decision when get_authoritative_sku was never successfully called."""

    task = _task()
    catalog_version_id = uuid.uuid4()
    sku = _sku(task, catalog_version_id)
    decision = _match_decision(task, sku, catalog_version_id)
    runtime = _Runtime(tool_calls_by_name={"retrieve_skus": 1})

    issues = validate_sku_mapping_decision(task, decision, runtime)
    assert any(issue.code == "authoritative_lookup_not_performed" for issue in issues)


def test_match_with_unconfirmed_sku_id_is_detected_and_correctable():
    """Reject a MATCH naming a sku_id the runtime never actually confirmed."""

    task = _task()
    catalog_version_id = uuid.uuid4()
    sku = _sku(task, catalog_version_id)
    decision = _match_decision(task, sku, catalog_version_id)
    runtime = _Runtime(tool_calls_by_name={"retrieve_skus": 1, "get_authoritative_sku": 1}, authoritative_skus_by_id={})

    issues = validate_sku_mapping_decision(task, decision, runtime)
    assert any(issue.code == "authoritative_sku_not_confirmed" and issue.correctable for issue in issues)


def test_match_with_mismatched_confirmed_code_or_name_is_detected():
    """Reject a MATCH whose sku_code/sku_name disagree with the confirmed authoritative record."""

    task = _task()
    catalog_version_id = uuid.uuid4()
    sku = _sku(task, catalog_version_id)
    decision = _match_decision(task, sku, catalog_version_id, sku_name="A Different Name")
    runtime = _Runtime(tool_calls_by_name={"retrieve_skus": 1, "get_authoritative_sku": 1}, authoritative_skus_by_id={sku.id: sku})

    issues = validate_sku_mapping_decision(task, decision, runtime)
    assert any(issue.code == "sku_identity_mismatch" and not issue.correctable for issue in issues)


def test_match_with_confirmed_sku_from_another_tenant_is_detected():
    """Reject a MATCH whose confirmed authoritative record belongs to a different organization."""

    task = _task()
    catalog_version_id = uuid.uuid4()
    sku = _sku(task, catalog_version_id, organization_id=uuid.uuid4())
    decision = _match_decision(task, sku, catalog_version_id)
    runtime = _Runtime(tool_calls_by_name={"retrieve_skus": 1, "get_authoritative_sku": 1}, authoritative_skus_by_id={sku.id: sku})

    issues = validate_sku_mapping_decision(task, decision, runtime)
    assert any(issue.code == "sku_tenant_mismatch" and not issue.correctable for issue in issues)


def test_match_with_confirmed_sku_from_another_catalog_version_is_detected():
    """Reject a MATCH whose catalog_version_id disagrees with the confirmed record's own."""

    task = _task()
    catalog_version_id = uuid.uuid4()
    sku = _sku(task, catalog_version_id)
    decision = _match_decision(task, sku, uuid.uuid4())
    runtime = _Runtime(tool_calls_by_name={"retrieve_skus": 1, "get_authoritative_sku": 1}, authoritative_skus_by_id={sku.id: sku})

    issues = validate_sku_mapping_decision(task, decision, runtime)
    assert any(issue.code == "sku_catalog_version_mismatch" and not issue.correctable for issue in issues)


def test_match_with_inactive_confirmed_sku_is_detected():
    """Reject a MATCH whose confirmed authoritative record is not active."""

    task = _task()
    catalog_version_id = uuid.uuid4()
    sku = _sku(task, catalog_version_id, is_active=False)
    decision = _match_decision(task, sku, catalog_version_id)
    runtime = _Runtime(tool_calls_by_name={"retrieve_skus": 1, "get_authoritative_sku": 1}, authoritative_skus_by_id={sku.id: sku})

    issues = validate_sku_mapping_decision(task, decision, runtime)
    assert any(issue.code == "sku_inactive" and not issue.correctable for issue in issues)
