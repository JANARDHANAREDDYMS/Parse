"""Define the versioned SKU-mapping input contract and narrow interface boundaries.

`SkuMappingTask` is built from one already-validated `DocumentAnalysisResult`
product candidate plus its linked commercial-status assessment. Building a task
here makes no eligibility decision about whether that candidate *should* be
sent to mapping — a later, separately configurable eligibility policy owns
that decision. This module performs no database access, no retrieval scoring,
and no Claude call.
"""

import uuid
from typing import Protocol

from pydantic import Field, model_validator

from maximor.document_analysis.schemas import (
    CommercialStatus,
    DocumentAnalysisResult,
    EvidenceReference,
    Identifier,
    MAX_EVIDENCE_PER_ENTITY,
)
from maximor.document_analysis.tool_schemas import BlockResult, EvidenceRegionInput, TableResult
from maximor.sku_mapping.errors import SkuMappingTaskConstructionError
from maximor.sku_mapping.schemas import CatalogVersionRecord, RetrievedSku, SkuMappingModel, SkuRecord, SkuRetrievalResult
from maximor.sku_mapping.tool_schemas import GetAuthoritativeSkuInput, RetrieveSkusInput


class SkuMappingTask(SkuMappingModel):
    """Carry one candidate's raw facts, commercial status, and evidence for mapping.

    `candidate_evidence` and `commercial_status_evidence` are kept separate
    rather than flattened, because they have different provenance: the former
    grounds that the candidate exists in the document, the latter grounds why
    it was assessed as `commercial_status`. A mapping agent or reviewer needs
    to tell those apart.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    analysis_run_id: uuid.UUID
    document_analysis_schema_version: str = Field(min_length=1, max_length=50)
    document_analysis_agent_version: str = Field(min_length=1, max_length=100)
    candidate_id: Identifier
    raw_name: str = Field(min_length=1, max_length=2_000)
    raw_attributes: dict[str, str | None] = Field(default_factory=dict)
    commercial_status: CommercialStatus
    commercial_status_rationale: str | None = Field(default=None, max_length=4_000)
    candidate_evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=MAX_EVIDENCE_PER_ENTITY)
    commercial_status_evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=MAX_EVIDENCE_PER_ENTITY)

    @property
    def all_evidence(self) -> tuple[EvidenceReference, ...]:
        """Return a deterministic, deduplicated view across both evidence sources.

        Order is stable: `candidate_evidence` first, then any
        `commercial_status_evidence` entries not already present. This is a
        convenience for callers that only need "some grounding," not a
        replacement for the labelled fields.
        """

        seen: list[EvidenceReference] = list(self.candidate_evidence)
        for reference in self.commercial_status_evidence:
            if reference not in seen:
                seen.append(reference)
        return tuple(seen)

    @model_validator(mode="after")
    def evidence_is_grounded_and_consistent(self) -> "SkuMappingTask":
        """Require some grounding overall and reject evidence from another run."""

        if not self.candidate_evidence and not self.commercial_status_evidence:
            raise ValueError("a SKU-mapping task requires at least one evidence reference")
        for reference in (*self.candidate_evidence, *self.commercial_status_evidence):
            if reference.preprocessing_run_id != self.preprocessing_run_id:
                raise ValueError("task evidence must belong to the task's preprocessing run")
        return self


def build_sku_mapping_task(
    *,
    result: DocumentAnalysisResult,
    analysis_run_id: uuid.UUID,
    candidate_id: str,
    schema_version: str,
) -> SkuMappingTask:
    """Build one task for one candidate from an already-validated analysis result.

    Requires exactly one commercial-status assessment linked to `candidate_id`.
    Zero is not groundable and more than one is an unresolved upstream
    modelling ambiguity; both are construction errors here, not mapping
    decisions. This function makes no judgement about whether the resulting
    task is eligible for mapping (e.g. by commercial status) — that filtering
    is a separate, later policy.
    """

    candidate = next((item for item in result.product_candidates if item.candidate_id == candidate_id), None)
    if candidate is None:
        raise SkuMappingTaskConstructionError(
            "sku_mapping_candidate_not_found",
            "The requested product candidate was not found in the analysis result.",
        )
    linked_statuses = [item for item in result.commercial_statuses if item.candidate_id == candidate_id]
    if len(linked_statuses) == 0:
        raise SkuMappingTaskConstructionError(
            "sku_mapping_status_missing",
            "The product candidate has no linked commercial-status assessment.",
        )
    if len(linked_statuses) > 1:
        raise SkuMappingTaskConstructionError(
            "sku_mapping_status_ambiguous",
            "The product candidate has more than one linked commercial-status assessment.",
        )
    status = linked_statuses[0]

    return SkuMappingTask(
        schema_version=schema_version,
        organization_id=result.organization_id,
        document_id=result.document_id,
        preprocessing_run_id=result.preprocessing_run_id,
        analysis_run_id=analysis_run_id,
        document_analysis_schema_version=result.schema_version,
        document_analysis_agent_version=result.agent_version,
        candidate_id=candidate.candidate_id,
        raw_name=candidate.raw_name,
        raw_attributes=dict(candidate.raw_attributes),
        commercial_status=status.status,
        commercial_status_rationale=status.raw_rationale,
        candidate_evidence=candidate.evidence,
        commercial_status_evidence=status.evidence,
    )


class SkuRepository(Protocol):
    """Expose only read-only, tenant-scoped catalog access for retrieval.

    This is intentionally a bulk-fetch shape today: `list_active_skus` returns
    every active SKU in one tenant-scoped catalog version so a demo in-process
    retriever can score them. A future PostgreSQL-native implementation of
    `HybridSkuRetriever` (server-side trigram similarity, pgvector KNN) is not
    required to route through `list_active_skus` at all — it may hold its own
    repository reference with additional server-side search methods. Adding
    such methods later is additive and does not require changing this
    Protocol's existing methods or their callers.
    """

    async def get_active_catalog_version(self, organization_id: uuid.UUID) -> CatalogVersionRecord | None: ...

    async def list_active_skus(
        self, organization_id: uuid.UUID, catalog_version_id: uuid.UUID
    ) -> tuple[SkuRecord, ...]: ...


class SemanticSkuSource(Protocol):
    """Reserve an optional pgvector-backed semantic search seam for later.

    No implementation of this Protocol exists yet — nothing computes
    embeddings or makes an external call. `DeterministicHybridSkuRetriever`
    does invoke and merge whatever a supplied source returns, so a future
    embedding-backed implementation can be plugged in without changing
    `HybridSkuRetriever` or any of its callers. It returns `RetrievedSku`
    (not a bare `SkuRecord`) because a semantic hit must carry a score
    comparable to exact/alias/lexical hits to be merged fairly against them.
    """

    async def search(
        self, task: SkuMappingTask, catalog_version: CatalogVersionRecord, *, limit: int
    ) -> tuple[RetrievedSku, ...]: ...


class HybridSkuRetriever(Protocol):
    """Return one deterministic, ranked, reproducible retrieval for one task."""

    async def retrieve(self, task: SkuMappingTask, *, limit: int = 10) -> SkuRetrievalResult: ...


class EvidenceResolver(Protocol):
    """Expose only evidence-region resolution, reusing `document_analysis`'s own contract.

    `EvidenceRegionInput`/`BlockResult`/`TableResult` are imported directly
    from `document_analysis.tool_schemas` rather than redefined: evidence
    resolution reads from the same `document_blocks`/`document_tables` tables
    for the same `preprocessing_run_id` regardless of which agent is asking.
    """

    async def get_evidence_region(self, request: EvidenceRegionInput) -> BlockResult | TableResult: ...


class SkuMappingToolset(Protocol):
    """Expose exactly the three narrow, tenant-scoped tools a `SkuMappingAgent` needs.

    `retrieve_skus` takes no free-text query and no organization/candidate
    identifiers — a concrete implementation is built for one fixed
    `SkuMappingTask` and always scores that task, so the caller cannot
    redirect retrieval toward ungrounded text. `get_authoritative_sku` is
    expected to be scoped to whichever catalog version the same invocation's
    `retrieve_skus` call actually used, not independently re-resolved, so the
    two calls cannot silently disagree about which catalog snapshot was
    consulted.
    """

    async def retrieve_skus(self, request: RetrieveSkusInput) -> SkuRetrievalResult: ...

    async def get_authoritative_sku(self, request: GetAuthoritativeSkuInput) -> SkuRecord: ...

    async def get_evidence_region(self, request: EvidenceRegionInput) -> BlockResult | TableResult: ...
