"""Define the compact, LLM-facing draft document-analysis output contract.

`DocumentAnalysisAgent` extracts raw contract facts and evidence only; it no
longer reasons about, receives, or emits global-term applicability. Claude
submits this draft shape through the finalizer -- `DraftGlobalTerm` has no
`applicability_scope` or `applies_to_candidate_ids` fields at all, so the
model cannot supply or override them, even through another field (this model
forbids extra fields, like every other contract in this package).

`promote_draft_result` is the only path from an accepted draft to the
canonical `DocumentAnalysisResult`. It assigns every term
`ApplicabilityScope.UNKNOWN` and an empty candidate-id tuple deterministically,
server-side. A later, separate subsystem is responsible for resolving real
applicability; until then, `unknown` is the correct persisted state.
"""

import uuid

from pydantic import Field, model_validator

from maximor.document_analysis.schemas import (
    MAX_CONTRACT_SECTIONS,
    MAX_DOCUMENT_EVIDENCE,
    MAX_EVIDENCE_PER_ENTITY,
    MAX_GLOBAL_TERMS,
    MAX_PRICING_SECTIONS,
    MAX_PRODUCT_CANDIDATES,
    MAX_STATUS_ASSESSMENTS,
    AnalysisModel,
    ApplicabilityScope,
    CommercialStatusAssessment,
    ContractStructure,
    DocumentAnalysisResult,
    EvidenceReference,
    GlobalTerm,
    Identifier,
    PricingSection,
    ProductCandidate,
)


class DraftGlobalTerm(AnalysisModel):
    """Represent one raw contract term: an identifier, its name, value, and evidence.

    This is the only shape Claude may submit for a global term. It carries no
    scope or linkage field of any kind, and forbids any field beyond the four
    declared here, so a submission cannot smuggle one in under another name.
    """

    term_id: Identifier
    raw_name: str = Field(min_length=1, max_length=500)
    raw_value: str | None = Field(default=None, max_length=4_000)
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=MAX_EVIDENCE_PER_ENTITY)


class DraftDocumentAnalysisResult(AnalysisModel):
    """Mirror `DocumentAnalysisResult`'s shape for LLM submission, terms only in draft form.

    Every field except `global_terms` matches the canonical result exactly.
    The identifier-ordering and evidence-run checks are mirrored from
    `DocumentAnalysisResult.identifiers_are_unique_and_ordered`, minus the
    term-to-candidate reference check, which cannot apply to a shape with no
    candidate-id field.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    preprocessing_schema_version: str = Field(min_length=1, max_length=50)
    prompt_version: str = Field(min_length=1, max_length=100)
    agent_version: str = Field(min_length=1, max_length=100)
    contract_structure: tuple[ContractStructure, ...] = Field(default=(), max_length=MAX_CONTRACT_SECTIONS)
    pricing_sections: tuple[PricingSection, ...] = Field(default=(), max_length=MAX_PRICING_SECTIONS)
    global_terms: tuple[DraftGlobalTerm, ...] = Field(default=(), max_length=MAX_GLOBAL_TERMS)
    product_candidates: tuple[ProductCandidate, ...] = Field(default=(), max_length=MAX_PRODUCT_CANDIDATES)
    commercial_statuses: tuple[CommercialStatusAssessment, ...] = Field(default=(), max_length=MAX_STATUS_ASSESSMENTS)
    evidence_references: tuple[EvidenceReference, ...] = Field(default=(), max_length=MAX_DOCUMENT_EVIDENCE)

    @model_validator(mode="after")
    def identifiers_are_unique_and_ordered(self) -> "DraftDocumentAnalysisResult":
        """Require deterministic ascending IDs and consistent evidence run references."""

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


def promote_draft_result(draft: DraftDocumentAnalysisResult) -> DocumentAnalysisResult:
    """Deterministically convert an accepted draft into the canonical result shape.

    Every term receives `ApplicabilityScope.UNKNOWN` and an empty
    `applies_to_candidate_ids` tuple here -- never read from the draft, which
    has no such fields to read. Reconstructing through
    `DocumentAnalysisResult(...)` (not `model_construct`) re-runs the
    canonical model's own validators as a second, independent check.
    """

    canonical_terms = tuple(
        GlobalTerm(
            term_id=term.term_id,
            raw_name=term.raw_name,
            raw_value=term.raw_value,
            evidence=term.evidence,
            applicability_scope=ApplicabilityScope.UNKNOWN,
            applies_to_candidate_ids=(),
        )
        for term in draft.global_terms
    )
    return DocumentAnalysisResult(
        schema_version=draft.schema_version,
        organization_id=draft.organization_id,
        document_id=draft.document_id,
        preprocessing_run_id=draft.preprocessing_run_id,
        preprocessing_schema_version=draft.preprocessing_schema_version,
        prompt_version=draft.prompt_version,
        agent_version=draft.agent_version,
        contract_structure=draft.contract_structure,
        pricing_sections=draft.pricing_sections,
        global_terms=canonical_terms,
        product_candidates=draft.product_candidates,
        commercial_statuses=draft.commercial_statuses,
        evidence_references=draft.evidence_references,
    )


__all__ = ["DraftGlobalTerm", "DraftDocumentAnalysisResult", "promote_draft_result"]
