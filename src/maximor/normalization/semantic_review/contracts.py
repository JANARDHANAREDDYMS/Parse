"""Build server-owned semantic-review tasks from Stage 5A issues."""

import hashlib

from maximor.normalization.contracts import NormalizationInput
from maximor.normalization.schemas import FinalizationReadiness, FinalizationStatus, ReviewQueueClassification
from maximor.normalization.semantic_review.errors import SemanticReviewTaskError
from maximor.normalization.semantic_review.schemas import (
    NormalizationSemanticReviewRequest,
    NormalizationSemanticReviewTask,
    SemanticReviewTaskItem,
)
from maximor.normalization.semantic_review.versions import (
    SEMANTIC_REVIEW_AGENT_VERSION,
    SEMANTIC_REVIEW_PROMPT_VERSION,
    SEMANTIC_REVIEW_SCHEMA_VERSION,
    SEMANTIC_REVIEW_SKILL_VERSION,
)

ELIGIBLE_REVIEW_CLASSIFICATIONS = frozenset({
    ReviewQueueClassification.AMBIGUOUS_VALUE,
    ReviewQueueClassification.SEMANTIC_SCOPE_REQUIRED,
    ReviewQueueClassification.CONFLICTING_VALUES,
})


def review_item_id(code: str, location: str) -> str:
    """Derive a stable bounded identifier from application-authored issue metadata."""

    return "review:" + hashlib.sha256(f"{code}|{location}".encode()).hexdigest()[:24]


def _allowed_evidence(normalization_input: NormalizationInput, extraction, candidate_id: str | None, field_name: str | None) -> tuple[str, ...]:
    """Collect only evidence already attached to the relevant trusted field."""

    refs = []
    if candidate_id:
        line = next((item for item in extraction.line_items if item.source_candidate_id == candidate_id), None)
        if line is not None:
            if field_name and field_name in line.field_provenance:
                refs.extend(line.field_provenance[field_name].evidence)
            else:
                refs.extend(ref for provenance in line.field_provenance.values() for ref in provenance.evidence)
        candidate = next((item for item in normalization_input.document_analysis.product_candidates if item.candidate_id == candidate_id), None)
        if candidate:
            refs.extend(candidate.evidence)
        artifact = normalization_input.sku_mappings.get(candidate_id)
        if artifact:
            refs.extend(artifact.decision.evidence)
    ids = sorted({reference.block_id or reference.table_id for reference in refs if reference.block_id or reference.table_id})
    return tuple(ids[:20])


def build_semantic_review_task(
    normalization_input: NormalizationInput,
    extraction,
    readiness: FinalizationReadiness,
    *,
    normalization_schema_version: str | None = None,
) -> NormalizationSemanticReviewTask:
    """Build a bounded task from eligible Stage 5A issues only.

    Failed validation is never sent to an agent.  Review issues without
    application-approved evidence remain assignable so the reviewer can return
    `INSUFFICIENT_EVIDENCE`, but they cannot be used to claim support.
    """

    if readiness.status is not FinalizationStatus.REVIEW_REQUIRED:
        raise SemanticReviewTaskError("semantic_review_not_eligible", "Only review-required normalization output may be reviewed.")
    selected = [issue for issue in readiness.issues if not issue.hard and issue.classification in ELIGIBLE_REVIEW_CLASSIFICATIONS]
    if not selected:
        raise SemanticReviewTaskError("semantic_review_no_eligible_items", "No eligible semantic-review items are assigned.")
    selected.sort(key=lambda issue: (review_item_id(issue.code, issue.location), issue.code, issue.location))
    ids = tuple(review_item_id(issue.code, issue.location) for issue in selected)
    request = NormalizationSemanticReviewRequest(
        organization_id=normalization_input.organization_id,
        document_id=normalization_input.document_id,
        preprocessing_run_id=normalization_input.preprocessing_run_id,
        analysis_run_id=normalization_input.analysis_run_id,
        normalization_schema_version=normalization_schema_version or normalization_input.schema_version,
        finalization_policy_version=readiness.policy_version,
        semantic_review_schema_version=SEMANTIC_REVIEW_SCHEMA_VERSION,
        prompt_version=SEMANTIC_REVIEW_PROMPT_VERSION,
        skill_version=SEMANTIC_REVIEW_SKILL_VERSION,
        agent_version=SEMANTIC_REVIEW_AGENT_VERSION,
        review_item_ids=ids,
    )
    items = []
    for issue, item_id in zip(selected, ids):
        line = next((line for line in extraction.line_items if line.source_candidate_id == issue.candidate_id), None)
        items.append(SemanticReviewTaskItem(
            review_item_id=item_id,
            candidate_id=issue.candidate_id,
            field_name=issue.field_name,
            classification=issue.classification,
            state="present" if line is not None and issue.field_name and getattr(line, issue.field_name, None) is not None else "unresolved",
            sku_code=line.sku_code if line is not None else None,
            sku_name=line.sku_name if line is not None else None,
            allowed_evidence_ids=_allowed_evidence(normalization_input, extraction, issue.candidate_id, issue.field_name),
        ))
    return NormalizationSemanticReviewTask(request=request, items=tuple(items))
