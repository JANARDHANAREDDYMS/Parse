"""Test the demo deterministic hybrid retriever against in-memory and real catalog records.

No database, no Claude call, no PDF access. The "real 21-SKU catalog fixture"
referenced here is the supplied `sku_catalog.json`, read directly as data (not
as a PDF and not through the ingestion pipeline).
"""

import json
import uuid

import pytest

from maximor.config import PROJECT_ROOT
from maximor.document_analysis.schemas import CommercialStatus, EvidenceReference, EvidenceRepresentation
from maximor.preprocessing.schemas import ExtractionSource
from maximor.sku_mapping.contracts import SkuMappingTask
from maximor.sku_mapping.errors import ActiveCatalogVersionNotFoundError, InvalidRetrievalLimitError
from maximor.sku_mapping.retrieval import DeterministicHybridSkuRetriever
from maximor.sku_mapping.schemas import CatalogVersionRecord, RetrievedSku, SkuMatchSource, SkuRecord
from maximor.sku_mapping.versions import SKU_MAPPING_TASK_SCHEMA_VERSION

CATALOG_FIXTURE_PATH = PROJECT_ROOT / "data" / "synthetic_order_form_dataset_50" / "sku_catalog.json"


class _FakeSkuRepository:
    """Serve fixed in-memory catalog rows without any database access."""

    def __init__(self, catalog_version: CatalogVersionRecord, skus: tuple[SkuRecord, ...]) -> None:
        self._catalog_version = catalog_version
        self._skus = skus

    async def get_active_catalog_version(self, organization_id: uuid.UUID) -> CatalogVersionRecord | None:
        if self._catalog_version.organization_id != organization_id:
            return None
        return self._catalog_version

    async def list_active_skus(self, organization_id: uuid.UUID, catalog_version_id: uuid.UUID) -> tuple[SkuRecord, ...]:
        return tuple(
            sku for sku in self._skus
            if sku.organization_id == organization_id and sku.catalog_version_id == catalog_version_id
        )


def _sku(catalog_version: CatalogVersionRecord, *, sku_code: str, name: str, description: str | None = None, aliases: tuple[str, ...] = ()) -> SkuRecord:
    return SkuRecord(
        id=uuid.uuid4(),
        source_sku_id=uuid.uuid4(),
        organization_id=catalog_version.organization_id,
        catalog_version_id=catalog_version.id,
        sku_code=sku_code,
        name=name,
        description=description,
        aliases=aliases,
    )


def _task(organization_id: uuid.UUID, *, raw_name: str, raw_attributes: dict[str, str | None] | None = None) -> SkuMappingTask:
    preprocessing_run_id = uuid.uuid4()
    evidence = EvidenceReference(
        preprocessing_run_id=preprocessing_run_id, page_number=1, block_id="native:p0001:b000000",
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    )
    return SkuMappingTask(
        schema_version=SKU_MAPPING_TASK_SCHEMA_VERSION,
        organization_id=organization_id,
        document_id=uuid.uuid4(),
        preprocessing_run_id=preprocessing_run_id,
        analysis_run_id=uuid.uuid4(),
        document_analysis_schema_version="analysis-v1",
        document_analysis_agent_version="agent-v1",
        candidate_id="candidate-0001",
        raw_name=raw_name,
        raw_attributes=raw_attributes or {},
        commercial_status=CommercialStatus.PURCHASED,
        candidate_evidence=(evidence,),
    )


@pytest.mark.asyncio
async def test_exact_name_match_scores_top_with_exact_source():
    """Rank an exact case/whitespace-insensitive name match first with score 1.0."""

    organization_id = uuid.uuid4()
    catalog_version = CatalogVersionRecord(id=uuid.uuid4(), organization_id=organization_id, version_identifier="v1", status="active")
    target = _sku(catalog_version, sku_code="PREMIUM_SUPPORT", name="Premium Support")
    decoy = _sku(catalog_version, sku_code="PROFESSIONAL_SERVICES", name="Professional Services")
    retriever = DeterministicHybridSkuRetriever(_FakeSkuRepository(catalog_version, (target, decoy)))

    result = await retriever.retrieve(_task(organization_id, raw_name="  premium   support "))

    assert result.candidates[0].sku.sku_code == "PREMIUM_SUPPORT"
    assert result.candidates[0].score == 1.0
    assert SkuMatchSource.EXACT in result.candidates[0].matched_sources
    assert result.catalog_version_id == catalog_version.id
    assert result.catalog_version_identifier == "v1"


@pytest.mark.asyncio
async def test_exact_sku_code_match_with_underscores_normalizes():
    """Match a raw name against `sku_code` after folding underscores to spaces."""

    organization_id = uuid.uuid4()
    catalog_version = CatalogVersionRecord(id=uuid.uuid4(), organization_id=organization_id, version_identifier="v1", status="active")
    target = _sku(catalog_version, sku_code="DATA_ANALYTICS_ADDON", name="Data Analytics Add-on")
    retriever = DeterministicHybridSkuRetriever(_FakeSkuRepository(catalog_version, (target,)))

    result = await retriever.retrieve(_task(organization_id, raw_name="Data Analytics Addon"))

    assert result.candidates[0].sku.sku_code == "DATA_ANALYTICS_ADDON"
    assert result.candidates[0].score == 1.0
    assert SkuMatchSource.EXACT in result.candidates[0].matched_sources


@pytest.mark.asyncio
async def test_alias_match_is_labelled_distinctly_from_exact_name():
    """Match on a curated alias and tag it ALIAS even though the name differs."""

    organization_id = uuid.uuid4()
    catalog_version = CatalogVersionRecord(id=uuid.uuid4(), organization_id=organization_id, version_identifier="v1", status="active")
    target = _sku(catalog_version, sku_code="TALENT_ACQUISITION", name="Talent Acquisition", aliases=("TA Suite",))
    retriever = DeterministicHybridSkuRetriever(_FakeSkuRepository(catalog_version, (target,)))

    result = await retriever.retrieve(_task(organization_id, raw_name="TA Suite"))

    assert result.candidates[0].sku.sku_code == "TALENT_ACQUISITION"
    assert result.candidates[0].matched_sources == (SkuMatchSource.ALIAS,)
    assert result.candidates[0].score == 1.0


@pytest.mark.asyncio
async def test_lexical_trigram_match_scores_below_exact_and_is_labelled_lexical():
    """Score a near-miss spelling below 1.0 and tag it LEXICAL, not EXACT."""

    organization_id = uuid.uuid4()
    catalog_version = CatalogVersionRecord(id=uuid.uuid4(), organization_id=organization_id, version_identifier="v1", status="active")
    target = _sku(catalog_version, sku_code="PROFESSIONAL_SERVICES", name="Professional Services")
    retriever = DeterministicHybridSkuRetriever(_FakeSkuRepository(catalog_version, (target,)))

    result = await retriever.retrieve(_task(organization_id, raw_name="Professional Service"))

    assert len(result.candidates) == 1
    assert result.candidates[0].sku.sku_code == "PROFESSIONAL_SERVICES"
    assert result.candidates[0].matched_sources == (SkuMatchSource.LEXICAL,)
    assert 0.0 < result.candidates[0].score < 1.0


@pytest.mark.asyncio
async def test_unrelated_candidate_produces_no_matches():
    """Return an empty ranked list rather than a low-confidence forced guess."""

    organization_id = uuid.uuid4()
    catalog_version = CatalogVersionRecord(id=uuid.uuid4(), organization_id=organization_id, version_identifier="v1", status="active")
    target = _sku(catalog_version, sku_code="PREMIUM_SUPPORT", name="Premium Support")
    retriever = DeterministicHybridSkuRetriever(_FakeSkuRepository(catalog_version, (target,)))

    result = await retriever.retrieve(_task(organization_id, raw_name="Unrelated Widget Bundle"))

    assert result.candidates == ()


@pytest.mark.asyncio
async def test_ranking_is_stable_and_limit_truncates():
    """Sort by descending score then ascending sku_code, and honor the requested limit."""

    organization_id = uuid.uuid4()
    catalog_version = CatalogVersionRecord(id=uuid.uuid4(), organization_id=organization_id, version_identifier="v1", status="active")
    exact = _sku(catalog_version, sku_code="TALENT_ACQUISITION", name="Talent Acquisition")
    near_a = _sku(catalog_version, sku_code="TALENT_ACQUISITION_ADDON_A", name="Talent Acquisition Addon A")
    near_b = _sku(catalog_version, sku_code="TALENT_ACQUISITION_ADDON_B", name="Talent Acquisition Addon B")
    retriever = DeterministicHybridSkuRetriever(_FakeSkuRepository(catalog_version, (near_b, exact, near_a)))

    result = await retriever.retrieve(_task(organization_id, raw_name="Talent Acquisition"), limit=2)

    assert len(result.candidates) == 2
    assert result.candidates[0].sku.sku_code == "TALENT_ACQUISITION"
    # Tie-break for the remaining equal-scoring lexical matches is by ascending sku_code.
    assert result.candidates[1].sku.sku_code == "TALENT_ACQUISITION_ADDON_A"


@pytest.mark.asyncio
async def test_descriptive_raw_attribute_is_also_matched():
    """Match using a descriptive raw attribute, not only `raw_name`."""

    organization_id = uuid.uuid4()
    catalog_version = CatalogVersionRecord(id=uuid.uuid4(), organization_id=organization_id, version_identifier="v1", status="active")
    target = _sku(catalog_version, sku_code="PREMIUM_SUPPORT", name="Premium Support")
    retriever = DeterministicHybridSkuRetriever(_FakeSkuRepository(catalog_version, (target,)))

    result = await retriever.retrieve(
        _task(organization_id, raw_name="Line item 3", raw_attributes={"sku_description": "Premium Support"})
    )

    assert result.candidates[0].sku.sku_code == "PREMIUM_SUPPORT"
    assert result.candidates[0].score == 1.0


class _FakeSemanticSkuSource:
    """Return fixed, pre-scored hits without computing any embedding or calling out."""

    def __init__(self, hits: tuple[RetrievedSku, ...]) -> None:
        self._hits = hits

    async def search(self, task, catalog_version, *, limit: int) -> tuple[RetrievedSku, ...]:
        return self._hits[:limit]


@pytest.mark.asyncio
async def test_semantic_source_hit_is_merged_into_ranking_when_no_lexical_match_exists():
    """Surface a semantic-only hit even though exact/alias/lexical scoring found nothing."""

    organization_id = uuid.uuid4()
    catalog_version = CatalogVersionRecord(id=uuid.uuid4(), organization_id=organization_id, version_identifier="v1", status="active")
    target = _sku(catalog_version, sku_code="PREMIUM_SUPPORT", name="Premium Support")
    semantic_hit = RetrievedSku(sku=target, score=0.72, matched_sources=(SkuMatchSource.SEMANTIC,), matched_text=None)
    retriever = DeterministicHybridSkuRetriever(
        _FakeSkuRepository(catalog_version, (target,)), semantic_source=_FakeSemanticSkuSource((semantic_hit,))
    )

    result = await retriever.retrieve(_task(organization_id, raw_name="Totally unrelated wording"))

    assert len(result.candidates) == 1
    assert result.candidates[0].sku.sku_code == "PREMIUM_SUPPORT"
    assert result.candidates[0].score == 0.72
    assert result.candidates[0].matched_sources == (SkuMatchSource.SEMANTIC,)


@pytest.mark.asyncio
async def test_semantic_source_hit_merges_with_lexical_hit_on_the_same_sku():
    """Union sources and keep the higher score when both techniques agree on one SKU."""

    organization_id = uuid.uuid4()
    catalog_version = CatalogVersionRecord(id=uuid.uuid4(), organization_id=organization_id, version_identifier="v1", status="active")
    target = _sku(catalog_version, sku_code="PROFESSIONAL_SERVICES", name="Professional Services")
    semantic_hit = RetrievedSku(sku=target, score=0.55, matched_sources=(SkuMatchSource.SEMANTIC,), matched_text=None)
    retriever = DeterministicHybridSkuRetriever(
        _FakeSkuRepository(catalog_version, (target,)), semantic_source=_FakeSemanticSkuSource((semantic_hit,))
    )

    # "Professional Service" scores a LEXICAL match well above 0.55 (see the dedicated test above).
    result = await retriever.retrieve(_task(organization_id, raw_name="Professional Service"))

    assert len(result.candidates) == 1
    assert set(result.candidates[0].matched_sources) == {SkuMatchSource.LEXICAL, SkuMatchSource.SEMANTIC}
    assert result.candidates[0].score > 0.55


@pytest.mark.asyncio
async def test_retrieve_rejects_out_of_bounds_limit():
    """Fail explicitly on an invalid limit instead of a confusing downstream validation error."""

    organization_id = uuid.uuid4()
    catalog_version = CatalogVersionRecord(id=uuid.uuid4(), organization_id=organization_id, version_identifier="v1", status="active")
    retriever = DeterministicHybridSkuRetriever(_FakeSkuRepository(catalog_version, ()))

    with pytest.raises(InvalidRetrievalLimitError):
        await retriever.retrieve(_task(organization_id, raw_name="Anything"), limit=0)
    with pytest.raises(InvalidRetrievalLimitError):
        await retriever.retrieve(_task(organization_id, raw_name="Anything"), limit=51)


@pytest.mark.asyncio
async def test_missing_active_catalog_version_raises_explicitly():
    """Fail explicitly rather than silently retrieving against no catalog."""

    organization_id = uuid.uuid4()
    other_org_catalog_version = CatalogVersionRecord(id=uuid.uuid4(), organization_id=uuid.uuid4(), version_identifier="v1", status="active")
    retriever = DeterministicHybridSkuRetriever(_FakeSkuRepository(other_org_catalog_version, ()))

    with pytest.raises(ActiveCatalogVersionNotFoundError):
        await retriever.retrieve(_task(organization_id, raw_name="Anything"))


@pytest.mark.asyncio
async def test_retrieval_against_real_21_sku_catalog_fixture():
    """Retrieve against the supplied real catalog fixture, not a generated stand-in."""

    raw_records = json.loads(CATALOG_FIXTURE_PATH.read_text(encoding="utf-8"))
    organization_id = uuid.uuid4()
    catalog_version = CatalogVersionRecord(id=uuid.uuid4(), organization_id=organization_id, version_identifier="fixture-v1", status="active")
    skus = tuple(
        SkuRecord(
            id=uuid.uuid4(),
            source_sku_id=uuid.UUID(record["id"]),
            organization_id=organization_id,
            catalog_version_id=catalog_version.id,
            sku_code=record["sku_code"],
            name=record["name"],
            description=record.get("description"),
            parsing_instructions=record.get("parsing_instructions"),
        )
        for record in raw_records
    )
    assert len(skus) == 21
    retriever = DeterministicHybridSkuRetriever(_FakeSkuRepository(catalog_version, skus))

    result = await retriever.retrieve(_task(organization_id, raw_name="Talent Acquisition"))

    assert result.candidates[0].sku.sku_code == "TALENT_ACQUISITION"
    assert result.candidates[0].score == 1.0
    assert result.catalog_version_identifier == "fixture-v1"
