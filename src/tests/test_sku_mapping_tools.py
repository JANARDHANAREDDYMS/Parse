"""Test the SKU-mapping tool input schemas and the narrow-tool adapter.

No database, no Claude call. `PersistedSkuMappingTools`'s collaborators are
plain in-memory fakes; each collaborator's own real implementation is tested
separately (`test_sku_mapping_retrieval.py`, `test_sku_mapping_repository.py`,
and `document_analysis`'s own evidence-resolution tests).
"""

import uuid

import pytest
from pydantic import ValidationError

from maximor.document_analysis.schemas import (
    CommercialStatus,
    EvidenceReference,
    EvidenceRepresentation,
    ExtractionSource,
)
from maximor.document_analysis.tool_schemas import BlockResult, EvidenceRegionInput
from maximor.preprocessing.schemas import BoundingBox
from maximor.sku_mapping.contracts import SkuMappingTask
from maximor.sku_mapping.errors import AuthoritativeLookupBeforeRetrievalError, SkuNotFoundError
from maximor.sku_mapping.schemas import CatalogVersionRecord, SkuMatchSource, SkuRecord, SkuRetrievalResult
from maximor.sku_mapping.tool_schemas import GetAuthoritativeSkuInput, RetrieveSkusInput
from maximor.sku_mapping.tools import PersistedSkuMappingTools
from maximor.sku_mapping.versions import (
    HYBRID_SKU_RETRIEVER_VERSION,
    SKU_MAPPING_TASK_SCHEMA_VERSION,
    SKU_RETRIEVAL_SCHEMA_VERSION,
)


def test_retrieve_skus_input_bounds():
    """Accept the default and boundary limits and reject out-of-bounds values."""

    assert RetrieveSkusInput().limit == 10
    assert RetrieveSkusInput(limit=1).limit == 1
    assert RetrieveSkusInput(limit=50).limit == 50
    with pytest.raises(ValidationError):
        RetrieveSkusInput(limit=0)
    with pytest.raises(ValidationError):
        RetrieveSkusInput(limit=51)


def test_get_authoritative_sku_input_requires_exactly_one_key():
    """Reject a lookup naming both or neither of sku_id/sku_code."""

    with pytest.raises(ValidationError):
        GetAuthoritativeSkuInput()
    with pytest.raises(ValidationError):
        GetAuthoritativeSkuInput(sku_id=uuid.uuid4(), sku_code="X")
    assert GetAuthoritativeSkuInput(sku_id=uuid.uuid4()).sku_code is None
    assert GetAuthoritativeSkuInput(sku_code="X").sku_id is None


def _task(organization_id: uuid.UUID) -> SkuMappingTask:
    preprocessing_run_id = uuid.uuid4()
    evidence = EvidenceReference(
        preprocessing_run_id=preprocessing_run_id, page_number=1, block_id="native:p0001:b000000",
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    )
    return SkuMappingTask(
        schema_version=SKU_MAPPING_TASK_SCHEMA_VERSION, organization_id=organization_id,
        document_id=uuid.uuid4(), preprocessing_run_id=preprocessing_run_id, analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="a", document_analysis_agent_version="a",
        candidate_id="candidate-0001", raw_name="Premium Support",
        commercial_status=CommercialStatus.PURCHASED, candidate_evidence=(evidence,),
    )


def _sku(catalog_version: CatalogVersionRecord, *, sku_code: str) -> SkuRecord:
    return SkuRecord(
        id=uuid.uuid4(), source_sku_id=uuid.uuid4(), organization_id=catalog_version.organization_id,
        catalog_version_id=catalog_version.id, sku_code=sku_code, name=sku_code.replace("_", " ").title(),
    )


class _FakeRetriever:
    def __init__(self, result: SkuRetrievalResult) -> None:
        self._result = result
        self.calls: list[tuple[SkuMappingTask, int]] = []

    async def retrieve(self, task: SkuMappingTask, *, limit: int = 10) -> SkuRetrievalResult:
        self.calls.append((task, limit))
        return self._result


class _FakeRepository:
    def __init__(self, skus: tuple[SkuRecord, ...]) -> None:
        self._skus = skus

    async def get_active_catalog_version(self, organization_id):
        raise AssertionError("get_authoritative_sku must not independently resolve the active catalog version")

    async def list_active_skus(self, organization_id, catalog_version_id):
        return tuple(
            sku for sku in self._skus
            if sku.organization_id == organization_id and sku.catalog_version_id == catalog_version_id
        )


class _FakeEvidenceResolver:
    def __init__(self, block: BlockResult) -> None:
        self._block = block
        self.calls: list[EvidenceRegionInput] = []

    async def get_evidence_region(self, request: EvidenceRegionInput) -> BlockResult:
        self.calls.append(request)
        return self._block


def _fixture(organization_id: uuid.UUID):
    catalog_version = CatalogVersionRecord(id=uuid.uuid4(), organization_id=organization_id, version_identifier="v1", status="active")
    target = _sku(catalog_version, sku_code="PREMIUM_SUPPORT")
    retrieval_result = SkuRetrievalResult(
        schema_version=SKU_RETRIEVAL_SCHEMA_VERSION, retriever_version=HYBRID_SKU_RETRIEVER_VERSION,
        organization_id=organization_id, catalog_version_id=catalog_version.id,
        catalog_version_identifier=catalog_version.version_identifier, candidate_id="candidate-0001",
        candidates=(),
    )
    return catalog_version, target, retrieval_result


@pytest.mark.asyncio
async def test_retrieve_skus_delegates_to_retriever_with_the_fixed_task():
    """Score the instance's own task, ignoring any organization/candidate in the request."""

    organization_id = uuid.uuid4()
    task = _task(organization_id)
    catalog_version, target, retrieval_result = _fixture(organization_id)
    retriever = _FakeRetriever(retrieval_result)
    tools = PersistedSkuMappingTools(retriever, _FakeRepository((target,)), _FakeEvidenceResolver(None), task)

    result = await tools.retrieve_skus(RetrieveSkusInput(limit=5))

    assert result is retrieval_result
    assert retriever.calls == [(task, 5)]


@pytest.mark.asyncio
async def test_authoritative_lookup_before_retrieval_is_rejected():
    """Refuse an authoritative lookup with no prior retrieve_skus call in this instance."""

    organization_id = uuid.uuid4()
    task = _task(organization_id)
    catalog_version, target, retrieval_result = _fixture(organization_id)
    tools = PersistedSkuMappingTools(_FakeRetriever(retrieval_result), _FakeRepository((target,)), _FakeEvidenceResolver(None), task)

    with pytest.raises(AuthoritativeLookupBeforeRetrievalError):
        await tools.get_authoritative_sku(GetAuthoritativeSkuInput(sku_id=target.id))


@pytest.mark.asyncio
async def test_authoritative_lookup_uses_the_catalog_version_from_this_instances_own_retrieval():
    """Scope the lookup to whichever catalog version this instance's retrieve_skus call used."""

    organization_id = uuid.uuid4()
    task = _task(organization_id)
    catalog_version, target, retrieval_result = _fixture(organization_id)
    tools = PersistedSkuMappingTools(_FakeRetriever(retrieval_result), _FakeRepository((target,)), _FakeEvidenceResolver(None), task)
    await tools.retrieve_skus(RetrieveSkusInput())

    by_id = await tools.get_authoritative_sku(GetAuthoritativeSkuInput(sku_id=target.id))
    by_code = await tools.get_authoritative_sku(GetAuthoritativeSkuInput(sku_code=target.sku_code))

    assert by_id == target
    assert by_code == target


@pytest.mark.asyncio
async def test_authoritative_lookup_raises_when_sku_not_found():
    """Fail explicitly rather than returning an unrelated SKU or None."""

    organization_id = uuid.uuid4()
    task = _task(organization_id)
    catalog_version, target, retrieval_result = _fixture(organization_id)
    tools = PersistedSkuMappingTools(_FakeRetriever(retrieval_result), _FakeRepository((target,)), _FakeEvidenceResolver(None), task)
    await tools.retrieve_skus(RetrieveSkusInput())

    with pytest.raises(SkuNotFoundError):
        await tools.get_authoritative_sku(GetAuthoritativeSkuInput(sku_id=uuid.uuid4()))


@pytest.mark.asyncio
async def test_get_evidence_region_delegates_to_the_evidence_resolver():
    """Pass the request straight through to the reused document-analysis resolver."""

    organization_id = uuid.uuid4()
    task = _task(organization_id)
    catalog_version, target, retrieval_result = _fixture(organization_id)
    block = BlockResult(
        block_id="native:p0001:b000000", page_number=1, representation=EvidenceRepresentation.NATIVE_TEXT,
        extraction_source=ExtractionSource.NATIVE, reading_order=0, text="Premium Support",
        bounding_box=BoundingBox(x0=0, y0=0, x1=1, y1=1),
    )
    resolver = _FakeEvidenceResolver(block)
    tools = PersistedSkuMappingTools(_FakeRetriever(retrieval_result), _FakeRepository((target,)), resolver, task)
    request = EvidenceRegionInput(
        organization_id=organization_id, preprocessing_run_id=task.preprocessing_run_id,
        evidence=task.candidate_evidence[0],
    )

    result = await tools.get_evidence_region(request)

    assert result is block
    assert resolver.calls == [request]
