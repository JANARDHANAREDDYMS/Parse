"""Define versioned semantic-analysis output contracts with persisted evidence links.

These schemas receive raw semantic findings and return validated structured output.
They do not access documents, normalize money, decide SKUs, or call an LLM.
"""

import uuid
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from maximor.preprocessing.schemas import BoundingBox, ExtractionSource

Identifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]


class AnalysisModel(BaseModel):
    """Forbid undocumented semantic fields and keep result contracts immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceRepresentation(StrEnum):
    """Identify which persisted preprocessing representation supplied evidence."""

    NATIVE_TEXT = "native_text"
    LAYOUT = "layout"
    OCR = "ocr"
    TABLE = "table"


class CommercialStatus(StrEnum):
    """Classify commercial treatment without making a SKU decision.

    `PURCHASED` is charged/ordered; `INCLUDED` is supplied without an independent
    purchase; `OPTIONAL` is selectable; `EXCLUDED` is expressly not supplied;
    `MENTIONED` is informational only; `AMBIGUOUS` preserves unresolved evidence.
    """

    PURCHASED = "purchased"
    INCLUDED = "included"
    OPTIONAL = "optional"
    EXCLUDED = "excluded"
    MENTIONED = "mentioned"
    AMBIGUOUS = "ambiguous"


class EvidenceReference(AnalysisModel):
    """Point to one stable persisted block or table without embedding source content."""

    preprocessing_run_id: uuid.UUID
    page_number: Annotated[int, Field(ge=1)]
    block_id: Identifier | None = None
    table_id: Identifier | None = None
    bounding_box: BoundingBox | None = None
    representation: EvidenceRepresentation
    extraction_source: ExtractionSource | None = None

    @model_validator(mode="after")
    def one_persisted_target_is_present(self) -> "EvidenceReference":
        """Require exactly one persisted evidence target and compatible representation."""

        if (self.block_id is None) == (self.table_id is None):
            raise ValueError("evidence requires exactly one block_id or table_id")
        if self.table_id is not None and self.representation != EvidenceRepresentation.TABLE:
            raise ValueError("table evidence must use table representation")
        if self.table_id is None and self.representation == EvidenceRepresentation.TABLE:
            raise ValueError("table representation requires table_id")
        return self


class ContractStructure(AnalysisModel):
    """Describe raw document-level structural findings without normalization."""

    structure_id: Identifier
    raw_label: str | None = Field(default=None, max_length=500)
    raw_value: str | None = Field(default=None, max_length=10_000)
    evidence: tuple[EvidenceReference, ...] = ()


class PricingSection(AnalysisModel):
    """Represent a raw pricing-related section without money arithmetic."""

    section_id: Identifier
    raw_title: str | None = Field(default=None, max_length=500)
    raw_text: str | None = Field(default=None, max_length=20_000)
    evidence: tuple[EvidenceReference, ...] = ()


class GlobalTerm(AnalysisModel):
    """Represent one raw document-wide term without deterministic normalization."""

    term_id: Identifier
    raw_name: str
    raw_value: str | None = Field(default=None, max_length=10_000)
    evidence: tuple[EvidenceReference, ...] = ()


class ProductCandidate(AnalysisModel):
    """Represent a document-described product/service eligible for later SKU mapping.

    It intentionally has no SKU field: `SKU-MAPPING SUB SYSTEM AGENT` owns all
    authoritative SKU retrieval and mapping decisions.
    """

    candidate_id: Identifier
    raw_name: str = Field(min_length=1, max_length=2_000)
    raw_attributes: dict[str, str | None] = Field(default_factory=dict)
    evidence: tuple[EvidenceReference, ...] = ()

    @model_validator(mode="after")
    def raw_attributes_are_bounded(self) -> "ProductCandidate":
        """Keep variable raw attributes compact while rejecting SKU-like mapping fields."""

        if len(self.raw_attributes) > 50:
            raise ValueError("product candidate has too many raw attributes")
        forbidden = {"sku", "sku_id", "sku_code", "mapped_sku", "final_sku"}
        if forbidden.intersection(key.lower() for key in self.raw_attributes):
            raise ValueError("product candidates must not contain SKU mapping values")
        if any(not key or len(key) > 100 or (value is not None and len(value) > 10_000)
               for key, value in self.raw_attributes.items()):
            raise ValueError("product candidate raw attributes exceed safe bounds")
        return self


class CommercialStatusAssessment(AnalysisModel):
    """Attach a commercial status to a candidate or document fact with evidence."""

    assessment_id: Identifier
    status: CommercialStatus
    candidate_id: Identifier | None = None
    raw_rationale: str | None = Field(default=None, max_length=10_000)
    evidence: tuple[EvidenceReference, ...] = ()


class DocumentAnalysisResult(AnalysisModel):
    """Return one versioned raw semantic analysis with deterministic collection order."""

    schema_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    preprocessing_schema_version: str = Field(min_length=1, max_length=50)
    prompt_version: str = Field(min_length=1, max_length=100)
    agent_version: str = Field(min_length=1, max_length=100)
    contract_structure: tuple[ContractStructure, ...] = ()
    pricing_sections: tuple[PricingSection, ...] = ()
    global_terms: tuple[GlobalTerm, ...] = ()
    product_candidates: tuple[ProductCandidate, ...] = ()
    commercial_statuses: tuple[CommercialStatusAssessment, ...] = ()
    evidence_references: tuple[EvidenceReference, ...] = ()

    @model_validator(mode="after")
    def identifiers_are_unique_and_ordered(self) -> "DocumentAnalysisResult":
        """Require deterministic ascending IDs and candidate-linked status references."""

        collections = (
            self.contract_structure,
            self.pricing_sections,
            self.global_terms,
            self.product_candidates,
            self.commercial_statuses,
        )
        for items in collections:
            ids = [getattr(item, next(name for name in ("structure_id", "section_id", "term_id", "candidate_id", "assessment_id") if hasattr(item, name))) for item in items]
            if ids != sorted(ids) or len(ids) != len(set(ids)):
                raise ValueError("analysis result identifiers must be unique and ordered")
        candidate_ids = {item.candidate_id for item in self.product_candidates}
        if any(item.candidate_id is not None and item.candidate_id not in candidate_ids for item in self.commercial_statuses):
            raise ValueError("commercial status references an unknown product candidate")
        nested_evidence = list(self.evidence_references)
        for item in (*self.contract_structure, *self.pricing_sections, *self.global_terms,
                     *self.product_candidates, *self.commercial_statuses):
            nested_evidence.extend(item.evidence)
        if any(reference.preprocessing_run_id != self.preprocessing_run_id for reference in nested_evidence):
            raise ValueError("evidence references must belong to the analysis preprocessing run")
        return self
