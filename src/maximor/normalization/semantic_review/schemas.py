"""Frozen, bounded contracts for normalization semantic review.

These schemas carry only trusted identifiers, safe structured review context,
and stable evidence identifiers. They never contain a complete extraction,
document text, storage paths, SQL, or credentials.
"""

import uuid
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from maximor.document_analysis.schemas import Identifier
from maximor.normalization.schemas import NormalizationModel, ReviewQueueClassification

MAX_REVIEW_ITEMS = 100
MAX_EVIDENCE_IDS = 20
MAX_RATIONALE = 1_000


class SemanticReviewOutcome(StrEnum):
    """Bound the conclusions a reviewer may return."""

    SUPPORTED_INTERPRETATION = "supported_interpretation"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICT_UNRESOLVED = "conflict_unresolved"
    ROUTE_FOR_TARGETED_CORRECTION = "route_for_targeted_correction"


class SemanticReviewOwner(StrEnum):
    """Identify the upstream owner of a recommended correction."""

    DOCUMENT_ANALYSIS = "document_analysis"
    TERM_APPLICABILITY = "term_applicability"
    SKU_MAPPING = "sku_mapping"


class SemanticReviewTaskItem(NormalizationModel):
    """Describe one assigned review issue without embedding its source value."""

    review_item_id: Identifier
    term_id: Identifier | None = None
    candidate_id: Identifier | None = None
    field_name: str | None = Field(default=None, max_length=100)
    classification: ReviewQueueClassification
    state: str = Field(min_length=1, max_length=40)
    sku_code: str | None = Field(default=None, max_length=255)
    sku_name: str | None = Field(default=None, max_length=512)
    allowed_evidence_ids: tuple[Identifier, ...] = Field(default=(), max_length=MAX_EVIDENCE_IDS)


class NormalizationSemanticReviewRequest(NormalizationModel):
    """Bind one semantic-review execution to one trusted normalization output."""

    organization_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    analysis_run_id: uuid.UUID
    normalization_schema_version: str = Field(min_length=1, max_length=50)
    finalization_policy_version: str = Field(min_length=1, max_length=50)
    semantic_review_schema_version: str = Field(min_length=1, max_length=50)
    prompt_version: str = Field(min_length=1, max_length=50)
    skill_version: str = Field(min_length=1, max_length=50)
    agent_version: str = Field(min_length=1, max_length=50)
    review_item_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=MAX_REVIEW_ITEMS)

    @model_validator(mode="after")
    def review_ids_are_ordered(self) -> "NormalizationSemanticReviewRequest":
        """Require deterministic, duplicate-free assignment."""
        if tuple(self.review_item_ids) != tuple(sorted(self.review_item_ids)) or len(set(self.review_item_ids)) != len(self.review_item_ids):
            raise ValueError("review_item_ids must be unique and ordered")
        return self


class NormalizationSemanticReviewTask(NormalizationModel):
    """Server-built review task containing only eligible bounded items."""

    request: NormalizationSemanticReviewRequest
    items: tuple[SemanticReviewTaskItem, ...] = Field(min_length=1, max_length=MAX_REVIEW_ITEMS)

    @model_validator(mode="after")
    def items_match_request(self) -> "NormalizationSemanticReviewTask":
        """Ensure assignment is exact and deterministic."""
        ids = tuple(item.review_item_id for item in self.items)
        if ids != self.request.review_item_ids or len(set(ids)) != len(ids):
            raise ValueError("task items must exactly match request review_item_ids")
        return self


class SemanticReviewFinding(NormalizationModel):
    """Return one bounded finding; it recommends but cannot apply a correction."""

    review_item_id: Identifier
    outcome: SemanticReviewOutcome
    owner: SemanticReviewOwner | None = None
    candidate_id: Identifier | None = None
    field_name: str | None = Field(default=None, max_length=100)
    evidence_ids: tuple[Identifier, ...] = Field(default=(), max_length=MAX_EVIDENCE_IDS)
    rationale: str | None = Field(default=None, max_length=MAX_RATIONALE)


class NormalizationSemanticReviewResult(NormalizationModel):
    """Versioned ordered findings for exactly one assigned task."""

    schema_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    analysis_run_id: uuid.UUID
    normalization_schema_version: str = Field(min_length=1, max_length=50)
    finalization_policy_version: str = Field(min_length=1, max_length=50)
    prompt_version: str = Field(min_length=1, max_length=50)
    skill_version: str = Field(min_length=1, max_length=50)
    agent_version: str = Field(min_length=1, max_length=50)
    findings: tuple[SemanticReviewFinding, ...] = Field(max_length=MAX_REVIEW_ITEMS)

    @model_validator(mode="after")
    def findings_are_unique_and_ordered(self) -> "NormalizationSemanticReviewResult":
        """Reject duplicates and non-canonical order before task validation."""
        ids = tuple(item.review_item_id for item in self.findings)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise ValueError("findings must be unique and ordered by review_item_id")
        return self
