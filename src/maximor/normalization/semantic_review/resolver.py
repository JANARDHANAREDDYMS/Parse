"""Resolve normalization semantic-review evidence IDs through persisted evidence tools.

`NormalizationEvidenceResolver` implements `SemanticEvidenceResolver`
(`tools.py`) by indexing every trusted evidence reference already reachable
from a `NormalizationInput` and its finalized `FinalOrderFormExtraction`,
then delegating actual region lookup to an existing narrow `EvidenceResolver`
-- the same `get_evidence_region` boundary `document_analysis`/`sku_mapping`/
`term_applicability` already reuse for their own narrow evidence tools. It
never accepts an unauthorized evidence ID: only IDs already present in the
trusted index resolve to anything, and `NormalizationSemanticReviewTools`
already confirms an ID belongs to the requested review item's own
`allowed_evidence_ids` before ever calling this resolver.
"""

from typing import Protocol

from maximor.document_analysis.schemas import EvidenceReference
from maximor.document_analysis.tool_schemas import BlockResult, EvidenceRegionInput, TableResult
from maximor.normalization.contracts import NormalizationInput
from maximor.normalization.schemas import FinalOrderFormExtraction
from maximor.normalization.semantic_review.errors import SemanticReviewTaskError


class EvidenceResolver(Protocol):
    """Expose only evidence-region resolution, reusing the existing narrow boundary."""

    async def get_evidence_region(self, request: EvidenceRegionInput) -> BlockResult | TableResult: ...


def _add(index: dict[str, EvidenceReference], references) -> None:
    """Merge one evidence group into the index, keyed by its own block/table ID."""

    for reference in references:
        identifier = reference.block_id or reference.table_id
        if identifier:
            index[identifier] = reference


def index_evidence_by_id(normalization_input: NormalizationInput, extraction: FinalOrderFormExtraction) -> dict[str, EvidenceReference]:
    """Build one identifier-to-reference lookup from every trusted source this task can cite.

    Deliberately built from the raw upstream sources (product candidates,
    accepted SKU-mapping decisions, raw commercial facts, coverage
    declarations, and term-applicability decisions) as well as the finalized
    extraction's own field/price-schedule/order-metadata provenance -- a
    superset is safe because `NormalizationSemanticReviewTools` only ever
    calls `resolve` with an ID already confirmed to be in one review item's
    own `allowed_evidence_ids`.
    """

    index: dict[str, EvidenceReference] = {}
    for candidate in normalization_input.document_analysis.product_candidates:
        _add(index, candidate.evidence)
    for artifact in normalization_input.sku_mappings.values():
        _add(index, artifact.decision.evidence)
    for bundle in normalization_input.term_applicability.candidate_commercial_facts:
        for fact in bundle.facts:
            _add(index, fact.evidence)
    for coverage in normalization_input.term_applicability.candidate_commercial_fact_coverage:
        _add(index, coverage.evidence)
    for decision in normalization_input.term_applicability.decisions:
        _add(index, decision.evidence)
    for item in extraction.line_items:
        for provenance in item.field_provenance.values():
            _add(index, provenance.evidence)
        for entry in item.yearly_price_schedule:
            _add(index, entry.provenance.evidence)
    if extraction.order_metadata is not None:
        for provenance in extraction.order_metadata.field_provenance.values():
            _add(index, provenance.evidence)
    return index


class NormalizationEvidenceResolver:
    """Resolve one already-authorized evidence ID against a trusted, bounded index."""

    def __init__(self, normalization_input: NormalizationInput, extraction: FinalOrderFormExtraction, resolver: EvidenceResolver) -> None:
        """Bind to one trusted input/extraction pair and one shared evidence resolver."""

        self._organization_id = normalization_input.organization_id
        self._preprocessing_run_id = normalization_input.preprocessing_run_id
        self._evidence_by_id = index_evidence_by_id(normalization_input, extraction)
        self._resolver = resolver

    async def resolve(self, evidence_id: str) -> BlockResult | TableResult:
        """Resolve one evidence ID already authorized by the caller's own allowlist check."""

        evidence = self._evidence_by_id.get(evidence_id)
        if evidence is None:
            raise SemanticReviewTaskError("evidence_not_indexed", "The requested evidence is unavailable.")
        return await self._resolver.get_evidence_region(
            EvidenceRegionInput(organization_id=self._organization_id, preprocessing_run_id=self._preprocessing_run_id, evidence=evidence)
        )
