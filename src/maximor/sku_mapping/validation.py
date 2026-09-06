"""Deterministically validate a proposed SKU-mapping decision before acceptance.

This module receives a trusted task and a proposed decision, returns safe
structured issues, and does not call Claude, access a database, or persist
anything. It decides only whether a decision is safe to accept as complete —
never whether the SKU choice itself is correct.
"""

import uuid
from dataclasses import dataclass
from typing import Protocol

from maximor.sku_mapping.contracts import SkuMappingTask
from maximor.sku_mapping.schemas import SkuMappingDecision, SkuMappingOutcome, SkuRecord
from maximor.sku_mapping.versions import SKU_MAPPING_DECISION_SCHEMA_VERSION


@dataclass(frozen=True)
class SkuMappingValidationIssue:
    """Describe one safe output defect and whether a narrow correction may address it."""

    code: str
    location: str
    message: str
    correctable: bool


class SkuMappingCompletionRuntime(Protocol):
    """Expose only successful tool-call facts needed by the completion gate.

    `authoritative_skus_by_id` holds the exact `SkuRecord` returned by each
    successful `get_authoritative_sku` call this invocation made, keyed by
    that SKU's id. Validation cross-checks a claimed `MATCH` against this
    captured record instead of re-querying the database itself — the same
    "trust a successful narrow-tool execution, don't re-fetch" pattern
    `document_analysis`'s completion gate already uses.
    """

    tool_calls_by_name: dict[str, int]
    authoritative_skus_by_id: dict[uuid.UUID, SkuRecord]


def validate_sku_mapping_decision(
    task: SkuMappingTask, decision: SkuMappingDecision,
    runtime: SkuMappingCompletionRuntime | None = None,
) -> tuple[SkuMappingValidationIssue, ...]:
    """Check trusted identity, retrieval/confirmation completion, and SKU integrity."""

    issues: list[SkuMappingValidationIssue] = []
    successful = runtime.tool_calls_by_name if runtime is not None else {}
    confirmed = runtime.authoritative_skus_by_id if runtime is not None else {}

    if (
        decision.organization_id, decision.document_id, decision.preprocessing_run_id,
        decision.analysis_run_id, decision.candidate_id,
    ) != (
        task.organization_id, task.document_id, task.preprocessing_run_id,
        task.analysis_run_id, task.candidate_id,
    ):
        issues.append(SkuMappingValidationIssue(
            "identity_mismatch", "decision", "Decision identifiers do not match the trusted task.", False,
        ))

    if decision.schema_version != SKU_MAPPING_DECISION_SCHEMA_VERSION:
        issues.append(SkuMappingValidationIssue(
            "version_mismatch", "decision", "Decision schema version does not match the expected version.", False,
        ))

    if successful.get("retrieve_skus", 0) < 1:
        issues.append(SkuMappingValidationIssue(
            "retrieval_not_performed", "runtime", "A decision requires at least one successful SKU retrieval.", False,
        ))

    task_evidence = list(task.all_evidence)
    if any(reference not in task_evidence for reference in decision.evidence):
        issues.append(SkuMappingValidationIssue(
            "evidence_not_grounded_in_task", "decision:evidence",
            "Decision evidence must come from the task's own candidate or status evidence.", True,
        ))

    if decision.outcome is SkuMappingOutcome.MATCH:
        if successful.get("get_authoritative_sku", 0) < 1:
            issues.append(SkuMappingValidationIssue(
                "authoritative_lookup_not_performed", "decision",
                "A match decision requires a successful authoritative SKU lookup.", False,
            ))
        authoritative = confirmed.get(decision.sku_id) if decision.sku_id is not None else None
        if authoritative is None:
            issues.append(SkuMappingValidationIssue(
                "authoritative_sku_not_confirmed", "decision:sku_id",
                "The decided SKU was never confirmed by a successful authoritative lookup.", True,
            ))
        else:
            if authoritative.sku_code != decision.sku_code or authoritative.name != decision.sku_name:
                issues.append(SkuMappingValidationIssue(
                    "sku_identity_mismatch", "decision:sku_code",
                    "Decision sku_code/sku_name do not agree with the authoritative catalog record.", False,
                ))
            if authoritative.organization_id != task.organization_id:
                issues.append(SkuMappingValidationIssue(
                    "sku_tenant_mismatch", "decision:sku_id",
                    "The decided SKU does not belong to the task's organization.", False,
                ))
            if authoritative.catalog_version_id != decision.catalog_version_id:
                issues.append(SkuMappingValidationIssue(
                    "sku_catalog_version_mismatch", "decision:catalog_version_id",
                    "The decided SKU does not belong to the decision's catalog version.", False,
                ))
            if not authoritative.is_active:
                issues.append(SkuMappingValidationIssue(
                    "sku_inactive", "decision:sku_id", "The decided SKU is not active in the catalog.", False,
                ))

    return tuple(issues)
