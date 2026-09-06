"""Deterministically validate proposed analysis output before persistence.

This module receives a trusted request and result, returns safe structured issues,
and does not call Claude, load documents, or alter semantic values.
"""

from dataclasses import dataclass
from typing import Protocol

from maximor.document_analysis.contracts import DocumentAnalysisRequest
from maximor.document_analysis.schemas import DocumentAnalysisResult


@dataclass(frozen=True)
class DocumentAnalysisValidationIssue:
    """Describe one safe output defect and whether a narrow correction may address it."""

    code: str
    location: str
    message: str
    correctable: bool


class CompletionRuntime(Protocol):
    """Expose only successful tool-call counts needed by the completion gate."""

    tool_calls_by_name: dict[str, int]


def validate_document_analysis_result(
    request: DocumentAnalysisRequest, result: DocumentAnalysisResult,
    runtime: CompletionRuntime | None = None,
) -> tuple[DocumentAnalysisValidationIssue, ...]:
    """Check trusted identity, successful grounding, evidence coverage, and no-SKU boundaries."""
    issues: list[DocumentAnalysisValidationIssue] = []
    successful = runtime.tool_calls_by_name if runtime is not None else {}
    if successful.get("get_document_overview", 0) < 1:
        issues.append(DocumentAnalysisValidationIssue("overview_not_retrieved", "runtime", "A completed analysis requires a successful document overview retrieval.", False))
    content_tools = ("search_document", "get_page_text", "get_page_blocks", "get_page_tables", "get_page_render")
    if sum(successful.get(name, 0) for name in content_tools) < 1:
        issues.append(DocumentAnalysisValidationIssue("document_content_not_retrieved", "runtime", "A completed analysis requires successful document-content retrieval.", False))
    if not any((result.contract_structure, result.pricing_sections, result.global_terms, result.product_candidates, result.commercial_statuses, result.evidence_references)):
        issues.append(DocumentAnalysisValidationIssue("empty_document_analysis", "result", "A completed analysis cannot contain no semantic findings.", False))
    if (result.organization_id, result.document_id, result.preprocessing_run_id) != (
        request.organization_id, request.document_id, request.preprocessing_run_id,
    ):
        issues.append(DocumentAnalysisValidationIssue("identity_mismatch", "result", "Result identifiers do not match the trusted request.", False))
    if (result.schema_version, result.preprocessing_schema_version, result.prompt_version, result.agent_version) != (
        request.document_analysis_schema_version, request.preprocessing_schema_version,
        request.prompt_version, request.agent_version,
    ):
        issues.append(DocumentAnalysisValidationIssue("version_mismatch", "result", "Result versions do not match the trusted request.", False))
    for label, values, id_field in (
        ("contract_structure", result.contract_structure, "structure_id"),
        ("pricing_section", result.pricing_sections, "section_id"),
        ("global_term", result.global_terms, "term_id"),
        ("product_candidate", result.product_candidates, "candidate_id"),
        ("commercial_status_assessment", result.commercial_statuses, "assessment_id"),
    ):
        for value in values:
            identity = getattr(value, id_field)
            if not value.evidence:
                issues.append(DocumentAnalysisValidationIssue("ungrounded_material_conclusion", f"{label}:{identity}", "Material output requires persisted evidence.", True))
    for candidate in result.product_candidates:
        forbidden = {"sku", "sku_id", "sku_code", "mapped_sku", "final_sku"}
        if forbidden.intersection(key.lower() for key in candidate.raw_attributes):
            issues.append(DocumentAnalysisValidationIssue("sku_mapping_present", f"product_candidate:{candidate.candidate_id}", "Product candidates cannot contain a SKU decision.", True))
    for reference in result.evidence_references:
        if reference.preprocessing_run_id != request.preprocessing_run_id:
            issues.append(DocumentAnalysisValidationIssue("evidence_run_mismatch", "evidence", "Evidence belongs to a different preprocessing run.", False))
    return tuple(issues)
