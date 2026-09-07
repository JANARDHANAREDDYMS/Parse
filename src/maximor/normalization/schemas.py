"""Define versioned final-output contracts for the eventual normalized order form.

These schemas are contracts only: `FinalOrderFormExtraction`,
`NormalizedOrderMetadata`, `NormalizedLineItem`, `MoneyValue`, and
`PriceScheduleEntry` describe the *shape* a later deterministic normalizer
must produce. Nothing here parses a date, computes a `Decimal` from raw text,
resolves currency, does arithmetic, or invents a default such as quantity
`1`. Fields are optional wherever a document may honestly omit a value --
absence here means "not yet normalized" or "the document did not state
this," never "assume a default."

This module performs no database access, no evidence resolution, and no
Claude call.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from maximor.document_analysis.schemas import MAX_EVIDENCE_PER_ENTITY, EvidenceReference, Identifier
from maximor.term_applicability.schemas import FactIdentifier

MAX_LINE_ITEMS = 500
MAX_PRICE_SCHEDULE_ENTRIES = 50
MAX_SPECIAL_NOTES = 50
MAX_REVIEW_ISSUES = 200
MAX_SKU_MAPPING_RUN_IDS = 500
MAX_FIELD_PROVENANCE_ENTRIES = 100


class NormalizationModel(BaseModel):
    """Forbid undocumented fields and keep normalization output contracts immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProvenanceSourceType(StrEnum):
    """Identify which upstream artifact a normalized value's provenance traces back to.

    `CANDIDATE_FACT` traces to a `RawCommercialFact` from term-applicability
    output; `DOCUMENT_TERM` traces to a `TermApplicabilityDecision` (a
    document-wide or candidate-scope global term); `SKU_MAPPING` traces to a
    `SkuMappingDecision`; `DERIVED` covers a value computed from one or more
    other already-provenanced values, by a later normalization stage --
    nothing in this stage produces a `DERIVED` value itself.
    """

    CANDIDATE_FACT = "candidate_fact"
    DOCUMENT_TERM = "document_term"
    SKU_MAPPING = "sku_mapping"
    DERIVED = "derived"


class ValueOrigin(StrEnum):
    """Classify how a normalized value came to exist, independent of its source type.

    `EXTRACTED` means read directly from one upstream source value.
    `INHERITED` means a document-wide term or fact was applied to a line
    item that did not itself carry the value -- e.g. a document-level
    invoicing term inherited onto every purchased candidate. `DERIVED` means
    computed from other already-normalized values (arithmetic, a schedule
    total). This stage does not populate any value, so it never assigns
    `INHERITED` or `DERIVED` itself; the enum exists so later stages have a
    stable vocabulary to record it in.
    """

    EXTRACTED = "extracted"
    INHERITED = "inherited"
    DERIVED = "derived"


class FieldProvenance(NormalizationModel):
    """Trace one normalized field back to its upstream source, bounded and evidence-only.

    Exactly one of `source_candidate_id`/`source_term_id`/`source_fact_id`
    identifies the specific upstream record this value traces to, matching
    `source_type`: `candidate_fact` requires `source_fact_id` (and
    `source_candidate_id`, since a fact always belongs to one candidate);
    `document_term`/`sku_mapping` require `source_term_id`/
    `source_candidate_id` respectively; `derived` requires neither, since it
    points at other already-normalized fields, not a single upstream record.
    `evidence` carries only stable evidence references, never raw document
    bodies, prompts, or SQL.
    """

    source_type: ProvenanceSourceType
    value_origin: ValueOrigin
    source_run_id: uuid.UUID | None = None
    source_candidate_id: Identifier | None = None
    source_term_id: Identifier | None = None
    source_fact_id: FactIdentifier | None = None
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=MAX_EVIDENCE_PER_ENTITY)

    @model_validator(mode="after")
    def identity_matches_source_type(self) -> "FieldProvenance":
        """Require the one identity field each source type names, and no other."""

        if self.source_type is ProvenanceSourceType.CANDIDATE_FACT:
            if self.source_fact_id is None or self.source_candidate_id is None:
                raise ValueError("candidate_fact provenance requires source_candidate_id and source_fact_id")
            if self.source_term_id is not None:
                raise ValueError("candidate_fact provenance must not carry source_term_id")
        elif self.source_type is ProvenanceSourceType.DOCUMENT_TERM:
            if self.source_term_id is None:
                raise ValueError("document_term provenance requires source_term_id")
            if self.source_fact_id is not None:
                raise ValueError("document_term provenance must not carry source_fact_id")
        elif self.source_type is ProvenanceSourceType.SKU_MAPPING:
            if self.source_candidate_id is None:
                raise ValueError("sku_mapping provenance requires source_candidate_id")
            if self.source_term_id is not None or self.source_fact_id is not None:
                raise ValueError("sku_mapping provenance must not carry source_term_id or source_fact_id")
        elif self.source_term_id is not None or self.source_fact_id is not None:
            raise ValueError("derived provenance must not carry source_term_id or source_fact_id")
        return self


class MoneyValue(NormalizationModel):
    """Represent one normalized monetary amount with an explicit ISO currency code.

    `amount` is a `Decimal`, never a float -- normalization arithmetic must
    stay decimal-safe. This stage never constructs one from raw text; that
    parsing belongs to a later deterministic normalizer.
    """

    amount: Decimal
    currency_code: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")


class PriceScheduleEntry(NormalizationModel):
    """Represent one period's price within a multi-period (e.g. yearly) price schedule.

    Mirrors `RawCommercialFactField.YEARLY_PRICE`'s period-repeating shape
    one level up: raw fields are preserved for traceability alongside
    whichever normalized amount a later stage fills in.
    """

    period_label: str | None = Field(default=None, min_length=1, max_length=100)
    period_start: date | None = None
    period_end: date | None = None
    raw_amount: str | None = Field(default=None, max_length=2_000)
    normalized_amount: MoneyValue | None = None
    provenance: FieldProvenance


class NormalizedOrderMetadata(NormalizationModel):
    """Represent document-level (non-line-item) normalized order facts.

    Every value is optional: a document may honestly omit any of these.
    `field_provenance` is keyed by this model's own field names (e.g.
    `"effective_date"`, `"total"`) and only ever contains an entry for a
    field this instance actually populated -- there is no provenance for an
    absent value.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    effective_date: date | None = None
    order_number: str | None = Field(default=None, max_length=200)
    quote_number: str | None = Field(default=None, max_length=200)
    customer_organization_name: str | None = Field(default=None, max_length=500)
    seller_organization_name: str | None = Field(default=None, max_length=500)
    bill_to: str | None = Field(default=None, max_length=2_000)
    contacts: tuple[str, ...] = Field(default=(), max_length=50)
    signatories: tuple[str, ...] = Field(default=(), max_length=50)
    currency: str | None = Field(default=None, min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    total: MoneyValue | None = None
    field_provenance: dict[str, FieldProvenance] = Field(default_factory=dict, max_length=MAX_FIELD_PROVENANCE_ENTRIES)


class NormalizedLineItem(NormalizationModel):
    """Represent one normalized purchased line item, traceable to its source candidate.

    Every commercial value is optional -- a field a later normalizer could
    not resolve stays `None` rather than receiving an invented default (a
    missing `quantity` is never assumed to be `1`). `field_provenance` is
    keyed the same way as `NormalizedOrderMetadata.field_provenance`.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    source_candidate_id: Identifier
    sku_id: uuid.UUID | None = None
    sku_code: str | None = Field(default=None, min_length=1, max_length=255)
    sku_name: str | None = Field(default=None, min_length=1, max_length=512)
    quantity: Decimal | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    unit_price: MoneyValue | None = None
    unit_price_period: str | None = Field(default=None, max_length=100)
    yearly_price_schedule: tuple[PriceScheduleEntry, ...] = Field(default=(), max_length=MAX_PRICE_SCHEDULE_ENTRIES)
    total_listed_value: MoneyValue | None = None
    service_start_date: date | None = None
    service_end_date: date | None = None
    invoicing_schedule_type: str | None = Field(default=None, max_length=100)
    invoicing_frequency: str | None = Field(default=None, max_length=100)
    payment_terms: str | None = Field(default=None, max_length=2_000)
    special_notes: tuple[str, ...] = Field(default=(), max_length=MAX_SPECIAL_NOTES)
    field_provenance: dict[str, FieldProvenance] = Field(default_factory=dict, max_length=MAX_FIELD_PROVENANCE_ENTRIES)


class ValidationStatus(StrEnum):
    """Classify a `FinalOrderFormExtraction`'s overall acceptance state.

    Mirrors the guarded-finalization discipline already settled for
    `document_analysis`/`sku_mapping`/`term_applicability`: `COMPLETED` is
    reserved for output that passed every required deterministic check;
    `REVIEW_REQUIRED` and `FAILED_VALIDATION` are both non-accepted states
    that still carry a persistable, auditable result.
    """

    COMPLETED = "completed"
    REVIEW_REQUIRED = "review_required"
    FAILED_VALIDATION = "failed_validation"


class ReviewIssueSeverity(StrEnum):
    """Bound the allowed severities for one review issue."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class FinalizationStatus(StrEnum):
    """State of the deterministic boundary before semantic review."""

    READY_FOR_SEMANTIC_REVIEW = "ready_for_semantic_review"
    REVIEW_REQUIRED = "review_required"
    FAILED_VALIDATION = "failed_validation"


class ReviewQueueClassification(StrEnum):
    """Bound the queue category used by a future semantic-review stage."""

    MISSING_VALUE = "missing_value"
    AMBIGUOUS_VALUE = "ambiguous_value"
    CONFLICTING_VALUES = "conflicting_values"
    RECONCILIATION_DIFFERENCE = "reconciliation_difference"
    UNSUPPORTED_FORMAT = "unsupported_format"
    SEMANTIC_SCOPE_REQUIRED = "semantic_scope_required"
    HARD_INVARIANT = "hard_invariant"


class FinalizationIssue(NormalizationModel):
    """Describe one bounded deterministic readiness issue without source content."""

    code: str = Field(min_length=1, max_length=100)
    classification: ReviewQueueClassification
    message: str = Field(min_length=1, max_length=500)
    location: str = Field(min_length=1, max_length=200)
    candidate_id: Identifier | None = None
    field_name: str | None = Field(default=None, max_length=100)
    hard: bool = False


class FinalizationReadiness(NormalizationModel):
    """Return the deterministic readiness outcome and ordered safe issues."""

    policy_version: str = Field(min_length=1, max_length=50)
    status: FinalizationStatus
    issues: tuple[FinalizationIssue, ...] = Field(default=(), max_length=MAX_REVIEW_ISSUES)

    @model_validator(mode="after")
    def status_matches_issues(self) -> "FinalizationReadiness":
        """Ensure the status cannot claim readiness while carrying issues."""

        if self.status is FinalizationStatus.READY_FOR_SEMANTIC_REVIEW and self.issues:
            raise ValueError("ready_for_semantic_review cannot contain issues")
        if self.status is FinalizationStatus.FAILED_VALIDATION and not any(issue.hard for issue in self.issues):
            raise ValueError("failed_validation requires a hard issue")
        return self


class ReviewIssue(NormalizationModel):
    """Represent one bounded, safe review finding attached to a final extraction.

    `message` is a short, application-authored description -- never raw
    document text, a prompt, or a stack trace.
    """

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1_000)
    severity: ReviewIssueSeverity
    candidate_id: Identifier | None = None
    field_name: str | None = Field(default=None, max_length=100)
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=MAX_EVIDENCE_PER_ENTITY)


class FinalOrderFormExtraction(NormalizationModel):
    """Return one versioned final normalized order-form extraction.

    This is a contract only in this stage: nothing in `maximor.normalization`
    yet constructs one from real assembled input. `sku_mapping_run_ids`
    carries every SKU-mapping run that contributed a line item, deduplicated
    and lexically ordered for determinism -- mirroring how every other
    result contract in this codebase orders its collections.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    analysis_run_id: uuid.UUID
    term_applicability_run_id: uuid.UUID
    sku_mapping_run_ids: tuple[uuid.UUID, ...] = Field(default=(), max_length=MAX_SKU_MAPPING_RUN_IDS)
    order_metadata: NormalizedOrderMetadata | None = None
    line_items: tuple[NormalizedLineItem, ...] = Field(default=(), max_length=MAX_LINE_ITEMS)
    validation_status: ValidationStatus = ValidationStatus.REVIEW_REQUIRED
    review_issues: tuple[ReviewIssue, ...] = Field(default=(), max_length=MAX_REVIEW_ISSUES)

    @model_validator(mode="after")
    def collections_are_unique_and_ordered(self) -> "FinalOrderFormExtraction":
        """Require deterministic, duplicate-free ordering for every collection."""

        run_ids = [str(value) for value in self.sku_mapping_run_ids]
        if run_ids != sorted(run_ids) or len(run_ids) != len(set(run_ids)):
            raise ValueError("sku_mapping_run_ids must be unique and ordered")
        candidate_ids = [item.source_candidate_id for item in self.line_items]
        if candidate_ids != sorted(candidate_ids) or len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("line_items must be unique and ordered by source_candidate_id")
        return self


class NormalizationRunStatus(StrEnum):
    """Domain terminal states owned by the normalization persistence service."""

    COMPLETED = "completed"
    REVIEW_REQUIRED = "review_required"
    FAILED_VALIDATION = "failed_validation"
    FAILED = "failed"


class NormalizationSemanticFinding(NormalizationModel):
    """Persistable bounded semantic-review finding snapshot."""

    review_item_id: Identifier
    outcome: str = Field(min_length=1, max_length=64)
    owner: str | None = Field(default=None, max_length=64)
    candidate_id: Identifier | None = None
    field_name: str | None = Field(default=None, max_length=100)
    evidence_ids: tuple[Identifier, ...] = Field(default=(), max_length=20)
    rationale: str | None = Field(default=None, max_length=1_000)


class NormalizationResult(NormalizationModel):
    """Canonical finalization result plus optional semantic-review findings."""

    schema_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    analysis_run_id: uuid.UUID
    term_applicability_run_id: uuid.UUID
    finalization_policy_version: str = Field(min_length=1, max_length=50)
    status: NormalizationRunStatus
    extraction: FinalOrderFormExtraction
    finalization_issues: tuple[FinalizationIssue, ...] = Field(default=(), max_length=MAX_REVIEW_ISSUES)
    semantic_findings: tuple[NormalizationSemanticFinding, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def lineage_matches_extraction(self) -> "NormalizationResult":
        """Keep the canonical wrapper and nested extraction on one lineage."""
        if (self.extraction.organization_id, self.extraction.document_id, self.extraction.preprocessing_run_id, self.extraction.analysis_run_id, self.extraction.term_applicability_run_id) != (self.organization_id, self.document_id, self.preprocessing_run_id, self.analysis_run_id, self.term_applicability_run_id):
            raise ValueError("normalization result lineage does not match extraction")
        return self
