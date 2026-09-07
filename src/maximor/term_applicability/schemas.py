"""Define versioned term-applicability decision, commercial-fact, and result contracts.

These schemas receive a proposed disposition/scope verdict per term, plus
proposed raw commercial facts per eligible candidate, and return validated
structured output. They perform no database access, no evidence resolution,
and no Claude call — `TermApplicabilityDecision`/`RawCommercialFact` are the
shapes `ClaudeTermApplicabilityAgent` must produce; nothing here decides or
extracts anything itself.
"""

import hashlib
import re
import uuid
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from maximor.document_analysis.schemas import (
    MAX_EVIDENCE_PER_ENTITY,
    MAX_GLOBAL_TERMS,
    MAX_PRODUCT_CANDIDATES,
    ApplicabilityScope,
    EvidenceReference,
    Identifier,
)

MAX_COMMERCIAL_FACTS_PER_CANDIDATE = 50
MAX_UNRESOLVED_FACT_FIELDS = 50

# Wider than `Identifier` (128 chars): a fact ID is derived by combining a
# candidate ID with a field name and, for period-bearing or free-text facts,
# a period/content fingerprint -- it needs headroom `Identifier` alone does
# not budget for, while keeping the same safe character set.
FactIdentifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")]


class TermApplicabilityModel(BaseModel):
    """Forbid undocumented fields and keep term-applicability contracts immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TermDisposition(StrEnum):
    """Classify whether a raw term is a line-item term at all before scoring its scope.

    `LINE_ITEM` covers commercially relevant terms that describe or qualify a
    purchase (dates, payment terms, currency, limits) and therefore need an
    `applicability_scope`. `DOCUMENT_METADATA` covers administrative facts —
    billing contact, signature, vendor name, quote number — that describe the
    document itself, not a line item; they are preserved, not scored or
    discarded.
    """

    LINE_ITEM = "line_item"
    DOCUMENT_METADATA = "document_metadata"


class TermApplicabilityDecision(TermApplicabilityModel):
    """Return one versioned, evidence-grounded applicability verdict for one term.

    This is the shape a future `TermApplicabilityAgent` must produce. A
    `line_item` disposition requires an `applicability_scope`: `candidate`
    requires one or more unique, lexically ordered `applies_to_candidate_ids`;
    `document` and `unknown` require none. `unknown` is a correct, preferred
    outcome when evidence cannot safely establish either relationship — it is
    not a placeholder for a missing answer. A `document_metadata` disposition
    carries no scope and no candidate IDs: it is not a line-item question at
    all. Evidence is required for every `line_item` decision, including
    `unknown` scope, because "we could not resolve this" is itself a material,
    evidence-grounded conclusion. Rationale is bounded and evidence-based only
    — never a channel for hidden reasoning.

    This type cannot express a candidate commercial fact of any kind, and a
    `RawCommercialFact` cannot express a term applicability claim of any kind
    — the two are structurally disjoint shapes with no shared identifier
    space (`term_id` vs `candidate_id`/`fact_id`), so a `document_metadata`
    term decision can never be "transformed into" a commercial fact; there is
    no field through which that transformation could even be represented.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    term_id: Identifier
    disposition: TermDisposition
    applicability_scope: ApplicabilityScope | None = None
    applies_to_candidate_ids: tuple[Identifier, ...] = ()
    rationale: str | None = Field(default=None, max_length=2_000)
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=MAX_EVIDENCE_PER_ENTITY)

    @model_validator(mode="after")
    def disposition_fields_are_internally_consistent(self) -> "TermApplicabilityDecision":
        """Enforce the field combinations each disposition does and does not permit."""

        ids = self.applies_to_candidate_ids
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise ValueError("applies_to_candidate_ids must be unique and lexically ordered")

        if self.disposition is TermDisposition.LINE_ITEM:
            if self.applicability_scope is None:
                raise ValueError("a line_item disposition requires an applicability_scope")
            if self.applicability_scope is ApplicabilityScope.CANDIDATE:
                if not ids:
                    raise ValueError("candidate scope requires at least one candidate id")
            elif ids:
                raise ValueError("document or unknown scope must not name candidate ids")
            if not self.evidence:
                raise ValueError("a line_item applicability decision requires at least one evidence reference")
        else:
            if self.applicability_scope is not None:
                raise ValueError("document_metadata must not carry an applicability_scope")
            if ids:
                raise ValueError("document_metadata must not carry candidate ids")
        return self


class RawCommercialFactField(StrEnum):
    """Bound the allowed raw commercial-fact field names.

    Every value is a *raw, unparsed* fact about one existing candidate.
    There is deliberately no field for a SKU reference, a normalized
    Decimal/ISO-date value, or any computed/derived quantity — those never
    belong to this agent. `YEARLY_PRICE` is the one field explicitly meant to
    repeat per candidate, once per `raw_period_label` (`Year 1`, `Year 2`,
    ...); every other field is expected to appear at most once per candidate
    unless a genuinely distinct period context justifies another instance.
    """

    QUANTITY = "quantity"
    CURRENCY = "currency"
    UNIT_PRICE = "unit_price"
    UNIT_PRICE_PERIOD = "unit_price_period"
    TOTAL_LISTED_VALUE = "total_listed_value"
    SERVICE_START_DATE = "service_start_date"
    SERVICE_END_DATE = "service_end_date"
    INVOICING_SCHEDULE_TYPE = "invoicing_schedule_type"
    INVOICING_FREQUENCY = "invoicing_frequency"
    PAYMENT_TERMS = "payment_terms"
    SPECIAL_NOTE = "special_note"
    YEARLY_PRICE = "yearly_price"


def _slug(value: str) -> str:
    """Return a bounded, safe-character fragment for a canonical fact ID."""

    cleaned = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned[:60] or "x"


def compute_fact_id(
    *,
    candidate_id: str,
    field: RawCommercialFactField,
    raw_value: str,
    raw_period_label: str | None = None,
    raw_period_start: str | None = None,
    raw_period_end: str | None = None,
) -> str:
    """Derive one deterministic, canonical, stable fact ID -- never supplied by Claude.

    The ID is a pure function of (candidate, field, period context) so that
    two facts naming the same field for the same candidate with the same
    period collide by construction -- which is exactly the rule "a candidate
    can have multiple facts of the same field only when their period
    contexts differ." `SPECIAL_NOTE` is the one field where multiple
    genuinely distinct instances have no natural period to distinguish them
    ("separate notes"), so its ID also folds in a short content fingerprint:
    two different notes get different IDs, but a byte-identical resubmission
    of the same note is still recognized as the same fact.
    """

    parts = [_slug(candidate_id), field.value]
    if raw_period_label:
        parts.append(_slug(raw_period_label))
    elif raw_period_start or raw_period_end:
        parts.append(_slug(f"{raw_period_start or ''}-{raw_period_end or ''}"))
    if field is RawCommercialFactField.SPECIAL_NOTE:
        parts.append(hashlib.sha256(raw_value.encode()).hexdigest()[:10])
    return "fact:" + ":".join(parts)


class RawCommercialFact(TermApplicabilityModel):
    """Represent one raw, evidence-backed commercial fact about one existing candidate.

    `raw_value` (and the optional raw period fields) are preserved exactly as
    written -- never parsed to `Decimal`, an ISO date, or a normalized
    billing enum; that normalization is explicitly a later stage's job, not
    this one's. `fact_id` is never supplied by Claude: the application
    computes it deterministically from the other fields via `compute_fact_id`
    and injects it, the same way trusted identity fields are injected
    elsewhere in this package. At least one evidence reference is required —
    an unsupported, invented fact is not representable here.
    """

    fact_id: FactIdentifier
    candidate_id: Identifier
    field: RawCommercialFactField
    raw_value: str = Field(min_length=1, max_length=2_000)
    raw_period_label: str | None = Field(default=None, min_length=1, max_length=100)
    raw_period_start: str | None = Field(default=None, min_length=1, max_length=100)
    raw_period_end: str | None = Field(default=None, min_length=1, max_length=100)
    evidence: tuple[EvidenceReference, ...] = Field(default=(), min_length=1, max_length=MAX_EVIDENCE_PER_ENTITY)

    @model_validator(mode="after")
    def fact_id_is_canonical(self) -> "RawCommercialFact":
        """Reject any fact whose ID does not match its own deterministic derivation."""

        expected = compute_fact_id(
            candidate_id=self.candidate_id, field=self.field, raw_value=self.raw_value,
            raw_period_label=self.raw_period_label, raw_period_start=self.raw_period_start,
            raw_period_end=self.raw_period_end,
        )
        if self.fact_id != expected:
            raise ValueError("fact_id must be the canonical identifier derived from this fact's own fields")
        return self


class CandidateCommercialFacts(TermApplicabilityModel):
    """Bundle every raw commercial fact extracted for one existing candidate.

    Membership of `candidate_id` against the task's own candidates, and
    candidate commercial-status eligibility (`purchased`/`included` only),
    are enforced by `build_term_applicability_result`/
    `validate_term_applicability_result` — this type only enforces its own
    internal shape: every fact must belong to this bundle's own
    `candidate_id`, and no two facts may share a `fact_id` (which, given
    `compute_fact_id`'s derivation, means no two facts may share the same
    field and period context).
    """

    candidate_id: Identifier
    facts: tuple[RawCommercialFact, ...] = Field(default=(), max_length=MAX_COMMERCIAL_FACTS_PER_CANDIDATE)

    @model_validator(mode="after")
    def facts_are_unique_and_match_candidate(self) -> "CandidateCommercialFacts":
        """Require every fact to belong to this candidate and carry a unique fact_id."""

        if any(fact.candidate_id != self.candidate_id for fact in self.facts):
            raise ValueError("every fact must belong to this bundle's own candidate_id")
        fact_ids = [fact.fact_id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("facts must not repeat the same fact_id")
        return self


class CandidateCommercialFactCoverage(TermApplicabilityModel):
    """Declare how each expected raw commercial field was resolved or left unresolved."""

    candidate_id: Identifier
    expected_fields: tuple[RawCommercialFactField, ...] = Field(default=(), max_length=MAX_UNRESOLVED_FACT_FIELDS)
    extracted_fields: tuple[RawCommercialFactField, ...] = Field(default=(), max_length=MAX_UNRESOLVED_FACT_FIELDS)
    unresolved_fields: tuple[RawCommercialFactField, ...] = Field(default=(), max_length=MAX_UNRESOLVED_FACT_FIELDS)
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=MAX_EVIDENCE_PER_ENTITY)

    @model_validator(mode="after")
    def fields_are_partitioned_and_ordered(self) -> "CandidateCommercialFactCoverage":
        """Require a deterministic, duplicate-free partition of expected fields."""

        groups = (self.expected_fields, self.extracted_fields, self.unresolved_fields)
        if any(tuple(items) != tuple(sorted(items, key=lambda item: item.value)) or len(items) != len(set(items)) for items in groups):
            raise ValueError("coverage fields must be unique and ordered")
        if set(self.extracted_fields) & set(self.unresolved_fields):
            raise ValueError("a field cannot be both extracted and unresolved")
        if set(self.extracted_fields) | set(self.unresolved_fields) != set(self.expected_fields):
            raise ValueError("coverage fields must partition expected_fields")
        if self.unresolved_fields and not self.evidence:
            raise ValueError("unresolved coverage requires evidence")
        return self


class TermApplicabilityResult(TermApplicabilityModel):
    """Return one versioned batch of term-applicability decisions and candidate facts.

    Membership of each decision's `term_id`/`applies_to_candidate_ids`, and
    of each `CandidateCommercialFacts.candidate_id`, against the originating
    `TermApplicabilityTask`'s loaded terms/candidates is enforced by
    `build_term_applicability_result` at construction time, not by a field on
    this type — mirroring how `build_sku_mapping_task` checks candidate
    existence externally rather than duplicating the whole analysis result
    inside every contract that needs to reference it. This type only
    enforces its own internal shape: unique, lexically ordered decisions and
    unique, lexically ordered per-candidate fact bundles.

    Adding `candidate_commercial_facts` is backwards compatible: it defaults
    to an empty tuple, so a term-only result persisted before this field
    existed (`schema_version` `"1.0.0"`) still deserializes here unchanged.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    analysis_run_id: uuid.UUID
    decisions: tuple[TermApplicabilityDecision, ...] = Field(default=(), max_length=MAX_GLOBAL_TERMS)
    candidate_commercial_facts: tuple[CandidateCommercialFacts, ...] = Field(default=(), max_length=MAX_PRODUCT_CANDIDATES)
    candidate_commercial_fact_coverage: tuple[CandidateCommercialFactCoverage, ...] = Field(default=(), max_length=MAX_PRODUCT_CANDIDATES)

    @model_validator(mode="after")
    def decisions_are_unique_and_ordered(self) -> "TermApplicabilityResult":
        """Require deterministic ascending term IDs with no duplicates."""

        term_ids = [decision.term_id for decision in self.decisions]
        if term_ids != sorted(term_ids) or len(term_ids) != len(set(term_ids)):
            raise ValueError("term-applicability decisions must be unique and ordered by term_id")
        candidate_ids = [bundle.candidate_id for bundle in self.candidate_commercial_facts]
        if candidate_ids != sorted(candidate_ids) or len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate_commercial_facts must be unique and ordered by candidate_id")
        coverage_ids = [item.candidate_id for item in self.candidate_commercial_fact_coverage]
        if coverage_ids != sorted(coverage_ids) or len(coverage_ids) != len(set(coverage_ids)):
            raise ValueError("candidate_commercial_fact_coverage must be unique and ordered by candidate_id")
        return self
