"""Define versioned SKU-retrieval and SKU-mapping-decision output contracts.

These schemas receive catalog rows, retrieval scores, and a proposed mapping
verdict and return validated structured output. They perform no database
access, no scoring, and no Claude call — `SkuMappingDecision` is the shape a
future `SkuMappingAgent` must produce; nothing here decides anything.
"""

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from maximor.document_analysis.schemas import MAX_EVIDENCE_PER_ENTITY, EvidenceReference, Identifier

MAX_RETRIEVED_SKUS = 50


class SkuMappingModel(BaseModel):
    """Forbid undocumented fields and keep SKU-mapping contracts immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SkuMatchSource(StrEnum):
    """Identify which retrieval technique proposed one SKU candidate.

    `SEMANTIC` is reserved for a future pgvector-backed source; nothing in the
    current foundation produces it.
    """

    EXACT = "exact"
    ALIAS = "alias"
    LEXICAL = "lexical"
    SEMANTIC = "semantic"


class CatalogVersionRecord(SkuMappingModel):
    """Identify one tenant-scoped catalog version exactly as persisted in `catalog_versions`."""

    id: uuid.UUID
    organization_id: uuid.UUID
    version_identifier: str = Field(min_length=1, max_length=100)
    status: str = Field(min_length=1, max_length=16)


class SkuRecord(SkuMappingModel):
    """Represent one tenant-scoped catalog row exactly as persisted in `skus`."""

    id: uuid.UUID
    source_sku_id: uuid.UUID
    organization_id: uuid.UUID
    catalog_version_id: uuid.UUID
    sku_code: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=512)
    description: str | None = None
    parsing_instructions: str | None = None
    aliases: tuple[str, ...] = Field(default=())
    is_active: bool = True


class RetrievedSku(SkuMappingModel):
    """Attach a deterministic match score and provenance to one catalog row."""

    sku: SkuRecord
    score: float = Field(ge=0.0, le=1.0)
    matched_sources: tuple[SkuMatchSource, ...] = Field(min_length=1)
    matched_text: str | None = Field(default=None, max_length=2_000)


class SkuRetrievalResult(SkuMappingModel):
    """Return one versioned, reproducible ranked retrieval for one mapping task.

    `catalog_version_id`/`catalog_version_identifier` are carried explicitly so
    a later mapping decision can always be traced back to the exact catalog
    snapshot it was retrieved against, independent of whichever catalog
    version is active at read time.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    retriever_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    catalog_version_id: uuid.UUID
    catalog_version_identifier: str = Field(min_length=1, max_length=100)
    candidate_id: Identifier
    candidates: tuple[RetrievedSku, ...] = Field(default=(), max_length=MAX_RETRIEVED_SKUS)


MAX_CONSIDERED_SKUS = 20


class SkuMappingOutcome(StrEnum):
    """Classify a mapping agent's verdict for one product candidate.

    `MATCH` selects exactly one authoritative catalog SKU; `NO_MATCH` states
    no catalog SKU applies; `AMBIGUOUS` preserves that evidence could not
    distinguish between two or more plausible SKUs, rather than forcing an
    unsupported guess.
    """

    MATCH = "match"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"


class SkuMappingDecision(SkuMappingModel):
    """Return one versioned, evidence-grounded mapping verdict for one candidate.

    This is the shape a future `SkuMappingAgent` must produce — it contains no
    scoring, no retrieval logic, and no persistence. `sku_id`/`sku_code`/
    `sku_name` are required together only for `MATCH` (all three, so a later
    check can independently confirm they agree with the authoritative catalog
    record rather than trusting the agent's word alone); `considered_sku_ids`
    is required (at least two, no repeats) only for `AMBIGUOUS`, naming what
    the evidence could not distinguish between. Evidence is required for every
    outcome, including `NO_MATCH` and `AMBIGUOUS` — those are still material
    conclusions about the candidate, not an absence of one.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    analysis_run_id: uuid.UUID
    candidate_id: Identifier
    catalog_version_id: uuid.UUID
    outcome: SkuMappingOutcome
    sku_id: uuid.UUID | None = None
    sku_code: str | None = Field(default=None, min_length=1, max_length=255)
    sku_name: str | None = Field(default=None, min_length=1, max_length=512)
    considered_sku_ids: tuple[uuid.UUID, ...] = Field(default=(), max_length=MAX_CONSIDERED_SKUS)
    rationale: str | None = Field(default=None, max_length=4_000)
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=MAX_EVIDENCE_PER_ENTITY)

    @model_validator(mode="after")
    def outcome_fields_are_internally_consistent(self) -> "SkuMappingDecision":
        """Enforce the field combinations each outcome does and does not permit."""

        if self.outcome is SkuMappingOutcome.MATCH:
            if self.sku_id is None or self.sku_code is None or self.sku_name is None:
                raise ValueError("a match decision requires sku_id, sku_code, and sku_name together")
        elif self.sku_id is not None or self.sku_code is not None or self.sku_name is not None:
            raise ValueError("only a match decision may carry sku_id, sku_code, or sku_name")

        if self.outcome is SkuMappingOutcome.AMBIGUOUS:
            if len(self.considered_sku_ids) < 2:
                raise ValueError("an ambiguous decision must list at least two considered SKUs")
            if len(self.considered_sku_ids) != len(set(self.considered_sku_ids)):
                raise ValueError("considered_sku_ids must not repeat")
        elif self.considered_sku_ids:
            raise ValueError("only an ambiguous decision may carry considered_sku_ids")

        if not self.evidence:
            raise ValueError("a SKU-mapping decision requires at least one evidence reference")
        if any(reference.preprocessing_run_id != self.preprocessing_run_id for reference in self.evidence):
            raise ValueError("decision evidence must belong to the decision's preprocessing run")
        return self
