"""Define the trusted normalization input contract and pure line-item source bundles.

`NormalizationInput` bundles one already-completed `DocumentAnalysisResult`,
one already-completed combined term-applicability artifact (triage decisions
plus applicability decisions/commercial facts/coverage), and one completed,
current (non-superseded) `SkuMappingRunArtifact` per commercially eligible
(`purchased`/`included`) product candidate. Building this input makes no
normalization decision of any kind -- no money/date/quantity parsing, no
arithmetic, no inheritance of document-wide terms onto line items. It only
confirms every source shares one tenant/document/analysis-run identity and
that every eligible candidate has the sources a later normalizer requires.

`LineItemSourceBundle` groups one candidate's sources for that later stage.
Document-wide terms are surfaced as `available_document_terms`, not merged
into the bundle's own facts -- inheriting them onto a line item is
explicitly a later normalization stage's decision, not this one's.

This module performs no database access, no evidence resolution, and no
Claude call.
"""

import uuid

from pydantic import Field, model_validator

from maximor.document_analysis.schemas import (
    MAX_EVIDENCE_PER_ENTITY,
    MAX_GLOBAL_TERMS,
    ApplicabilityScope,
    CommercialStatus,
    DocumentAnalysisResult,
    EvidenceReference,
    Identifier,
    ProductCandidate,
)
from maximor.normalization.errors import NormalizationInputConstructionError
from maximor.normalization.schemas import NormalizationModel
from maximor.sku_mapping.contracts import SkuMappingRunArtifact
from maximor.sku_mapping.schemas import SkuMappingDecision, SkuMappingOutcome
from maximor.term_applicability.contracts import FACT_ELIGIBLE_COMMERCIAL_STATUSES, RAW_ATTRIBUTE_FACT_HINTS, REQUIRED_CONTRACT_ITEM_FACT_FIELDS
from maximor.term_applicability.schemas import (
    CandidateCommercialFactCoverage,
    CandidateCommercialFacts,
    RawCommercialFactField,
    TermApplicabilityDecision,
    TermApplicabilityResult,
)
from maximor.term_triage.schemas import TermTriageResult


def expected_fact_fields(candidate: ProductCandidate, status: CommercialStatus) -> tuple[RawCommercialFactField, ...]:
    """Mirror `CandidateContext.expected_fact_fields` against a raw `ProductCandidate`.

    Reuses the same trusted hint mapping `term_applicability.contracts`
    already owns, rather than redefining a second copy of it here.
    """

    if status not in FACT_ELIGIBLE_COMMERCIAL_STATUSES:
        return ()
    hinted = {field for key, field in RAW_ATTRIBUTE_FACT_HINTS.items() if candidate.raw_attributes.get(key)}
    if not hinted:
        return ()
    return tuple(sorted(hinted | REQUIRED_CONTRACT_ITEM_FACT_FIELDS, key=lambda item: item.value))


def status_by_candidate_map(document_analysis: DocumentAnalysisResult) -> dict[str, CommercialStatus]:
    """Require exactly one commercial-status assessment per product candidate.

    Mirrors the same check `build_sku_mapping_task`/`build_term_applicability_task`
    already perform independently for their own callers -- a candidate with
    zero or more than one linked assessment is an unresolved upstream
    modelling ambiguity, not something normalization can silently paper over.
    """

    result: dict[str, CommercialStatus] = {}
    for candidate in document_analysis.product_candidates:
        linked = [
            item for item in document_analysis.commercial_statuses
            if item.candidate_id == candidate.candidate_id
        ]
        if len(linked) != 1:
            raise NormalizationInputConstructionError(
                "normalization_candidate_status_ambiguous",
                "A product candidate does not have exactly one linked commercial-status assessment.",
            )
        result[candidate.candidate_id] = linked[0].status
    return result


def eligible_candidate_ids_of(document_analysis: DocumentAnalysisResult) -> tuple[str, ...]:
    """Return every purchased/included candidate id, lexically ordered."""

    status_by_candidate = status_by_candidate_map(document_analysis)
    return tuple(sorted(
        candidate_id for candidate_id, status in status_by_candidate.items()
        if status in FACT_ELIGIBLE_COMMERCIAL_STATUSES
    ))


class NormalizationRequest(NormalizationModel):
    """Name exactly which completed sources one normalization pass must load.

    This is an input identifier bundle only -- it names runs, it does not
    load or validate them. `NormalizationRepository.load_normalization_input`
    resolves it into a validated `NormalizationInput`.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    document_id: uuid.UUID
    analysis_run_id: uuid.UUID
    term_applicability_run_id: uuid.UUID


class NormalizationInput(NormalizationModel):
    """Bundle every completed, mutually consistent source a normalizer needs.

    `sku_mappings` is keyed by external `candidate_id` and contains exactly
    one entry for every `purchased`/`included` candidate named in
    `document_analysis` -- an eligible candidate missing an entry can never
    exist here; `assemble_normalization_input` raises a typed error before
    constructing an instance that would violate this, and this type's own
    validator re-checks the same invariant independently. Entries may carry
    any `SkuMappingOutcome`: a `NO_MATCH`/`AMBIGUOUS` mapping is still a
    completed, honest answer worth keeping in the trusted input, even though
    only a `MATCH` entry can ever produce a `LineItemSourceBundle`.
    `sku_mapping_run_ids` mirrors `sku_mappings`'s own keys exactly, carrying
    the persisted run id behind each entry (the canonical artifact itself
    never states its own run id).
    """

    schema_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    analysis_run_id: uuid.UUID
    term_applicability_run_id: uuid.UUID
    document_analysis: DocumentAnalysisResult
    term_triage: TermTriageResult
    term_applicability: TermApplicabilityResult
    sku_mappings: dict[Identifier, SkuMappingRunArtifact] = Field(default_factory=dict)
    sku_mapping_run_ids: dict[Identifier, uuid.UUID] = Field(default_factory=dict)

    @property
    def eligible_candidate_ids(self) -> tuple[str, ...]:
        """Return every purchased/included candidate id, lexically ordered."""

        return eligible_candidate_ids_of(self.document_analysis)

    @model_validator(mode="after")
    def sources_share_identity_and_cover_every_eligible_candidate(self) -> "NormalizationInput":
        """Require one shared tenant/document/run identity and full eligible-candidate coverage."""

        if self.document_analysis.organization_id != self.organization_id or self.document_analysis.document_id != self.document_id:
            raise ValueError("document_analysis does not match this input's own organization/document identity")
        if self.document_analysis.preprocessing_run_id != self.preprocessing_run_id:
            raise ValueError("document_analysis does not match this input's own preprocessing_run_id")

        for source in (self.term_triage, self.term_applicability):
            if (
                source.organization_id != self.organization_id
                or source.document_id != self.document_id
                or source.preprocessing_run_id != self.preprocessing_run_id
                or source.analysis_run_id != self.analysis_run_id
            ):
                raise ValueError("term triage/applicability does not match this input's own source identity")

        eligible_ids = set(self.eligible_candidate_ids)
        if set(self.sku_mappings) != eligible_ids or set(self.sku_mapping_run_ids) != eligible_ids:
            raise ValueError("sku_mappings and sku_mapping_run_ids must cover exactly the eligible candidates")

        for candidate_id, artifact in self.sku_mappings.items():
            if (
                artifact.task.organization_id != self.organization_id
                or artifact.task.document_id != self.document_id
                or artifact.task.analysis_run_id != self.analysis_run_id
                or artifact.decision.candidate_id != candidate_id
            ):
                raise ValueError("a sku_mappings entry does not match this input's own identity or candidate")

        for bundle in self.term_applicability.candidate_commercial_facts:
            if bundle.candidate_id not in eligible_ids:
                raise ValueError("candidate_commercial_facts references a candidate outside the eligible set")

        candidates_by_id = {candidate.candidate_id: candidate for candidate in self.document_analysis.product_candidates}
        status_by_candidate = status_by_candidate_map(self.document_analysis)
        coverage_by_candidate = {item.candidate_id: item for item in self.term_applicability.candidate_commercial_fact_coverage}
        for candidate_id in eligible_ids:
            expected = expected_fact_fields(candidates_by_id[candidate_id], status_by_candidate[candidate_id])
            if expected and candidate_id not in coverage_by_candidate:
                raise ValueError("an eligible candidate with expected raw-fact hints has no commercial-fact coverage")

        return self


class LineItemSourceBundle(NormalizationModel):
    """Group one purchased/included candidate's exact sources for a later normalizer.

    `available_candidate_terms` is every applicability decision naming this
    candidate; `available_document_terms` is every document-scope decision
    -- both are exposed, not merged into `candidate_commercial_facts`, so a
    later normalization stage decides how (or whether) to inherit a
    document-wide term onto this line item. `evidence` is a deterministic,
    deduplicated union of every source's own evidence, for convenience only
    -- it never replaces the labelled evidence already attached to each
    nested source.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    candidate_id: Identifier
    commercial_status: CommercialStatus
    product_candidate: ProductCandidate
    sku_mapping_run_id: uuid.UUID
    sku_mapping_decision: SkuMappingDecision
    candidate_commercial_facts: CandidateCommercialFacts | None = None
    candidate_commercial_fact_coverage: CandidateCommercialFactCoverage | None = None
    available_candidate_terms: tuple[TermApplicabilityDecision, ...] = Field(default=(), max_length=MAX_GLOBAL_TERMS)
    available_document_terms: tuple[TermApplicabilityDecision, ...] = Field(default=(), max_length=MAX_GLOBAL_TERMS)
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=MAX_EVIDENCE_PER_ENTITY)

    @model_validator(mode="after")
    def bundle_is_internally_consistent(self) -> "LineItemSourceBundle":
        """Require every nested source to agree with this bundle's own candidate and commit to MATCH."""

        if self.product_candidate.candidate_id != self.candidate_id:
            raise ValueError("product_candidate does not match this bundle's own candidate_id")
        if self.commercial_status not in FACT_ELIGIBLE_COMMERCIAL_STATUSES:
            raise ValueError("a line-item source bundle requires a purchased or included commercial status")
        if self.sku_mapping_decision.candidate_id != self.candidate_id:
            raise ValueError("sku_mapping_decision does not match this bundle's own candidate_id")
        if self.sku_mapping_decision.outcome is not SkuMappingOutcome.MATCH:
            raise ValueError("a line-item source bundle requires a MATCH sku mapping decision")
        if self.candidate_commercial_facts is not None and self.candidate_commercial_facts.candidate_id != self.candidate_id:
            raise ValueError("candidate_commercial_facts does not match this bundle's own candidate_id")
        if self.candidate_commercial_fact_coverage is not None and self.candidate_commercial_fact_coverage.candidate_id != self.candidate_id:
            raise ValueError("candidate_commercial_fact_coverage does not match this bundle's own candidate_id")
        for decision in self.available_candidate_terms:
            if self.candidate_id not in decision.applies_to_candidate_ids:
                raise ValueError("available_candidate_terms contains a decision that does not name this candidate")
        for decision in self.available_document_terms:
            if decision.applicability_scope is not ApplicabilityScope.DOCUMENT:
                raise ValueError("available_document_terms contains a decision that is not document-scope")
        return self
