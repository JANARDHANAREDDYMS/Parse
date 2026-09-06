"""Implement the three narrow tools a future SkuMappingAgent will call.

`PersistedSkuMappingTools` wraps an already-configured `HybridSkuRetriever`,
`SkuRepository`, and evidence resolver. It performs no database access of its
own beyond delegating to those collaborators, and one instance is always
scoped to exactly one fixed `SkuMappingTask` — there is no operation that
lets a caller retrieve or confirm against a different candidate,
organization, or catalog version than the one it was built for.
"""

import uuid

from maximor.document_analysis.tool_schemas import BlockResult, EvidenceRegionInput, TableResult
from maximor.sku_mapping.contracts import EvidenceResolver, HybridSkuRetriever, SkuMappingTask, SkuRepository
from maximor.sku_mapping.errors import AuthoritativeLookupBeforeRetrievalError, SkuNotFoundError
from maximor.sku_mapping.schemas import SkuRecord, SkuRetrievalResult
from maximor.sku_mapping.tool_schemas import GetAuthoritativeSkuInput, RetrieveSkusInput


class PersistedSkuMappingTools:
    """Implement `SkuMappingToolset` for one fixed candidate task per instance.

    `get_authoritative_sku` is scoped to whichever catalog version this
    instance's own `retrieve_skus` call actually used — not independently
    re-resolved — so a decision's retrieval and its authoritative
    confirmation can never silently disagree about which catalog snapshot
    was consulted. Calling it before any `retrieve_skus` call is a explicit
    error, not an implicit fresh lookup.
    """

    def __init__(
        self,
        retriever: HybridSkuRetriever,
        repository: SkuRepository,
        evidence_resolver: EvidenceResolver,
        task: SkuMappingTask,
    ) -> None:
        """Receive already-configured collaborators and the one task this instance serves."""

        self._retriever = retriever
        self._repository = repository
        self._evidence_resolver = evidence_resolver
        self._task = task
        self._catalog_version_id: uuid.UUID | None = None

    async def retrieve_skus(self, request: RetrieveSkusInput) -> SkuRetrievalResult:
        """Score this instance's fixed trusted task and remember which catalog version was used."""

        result = await self._retriever.retrieve(self._task, limit=request.limit)
        self._catalog_version_id = result.catalog_version_id
        return result

    async def get_authoritative_sku(self, request: GetAuthoritativeSkuInput) -> SkuRecord:
        """Independently re-confirm one SKU against the catalog version already retrieved."""

        if self._catalog_version_id is None:
            raise AuthoritativeLookupBeforeRetrievalError
        skus = await self._repository.list_active_skus(self._task.organization_id, self._catalog_version_id)
        match = next(
            (sku for sku in skus if sku.id == request.sku_id or sku.sku_code == request.sku_code),
            None,
        )
        if match is None:
            raise SkuNotFoundError
        return match

    async def get_evidence_region(self, request: EvidenceRegionInput) -> BlockResult | TableResult:
        """Delegate evidence resolution to the existing document-analysis implementation."""

        return await self._evidence_resolver.get_evidence_region(request)
