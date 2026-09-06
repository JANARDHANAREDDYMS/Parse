"""Test the Postgres-backed SKU repository against generated tenant-scoped rows.

Uses the same live test-database pattern as `test_document_analysis_persistence.py`.
Never touches the supplied dataset catalog or any admin ingestion path.
"""

import uuid
from datetime import UTC, datetime

import pytest

from maximor.db.models import CatalogVersion, Sku
from maximor.db.models.statuses import CatalogStatus
from maximor.db.session import get_session_factory
from maximor.sku_mapping.repository import PostgresSkuRepository


async def _seed_catalog(organization_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create one active catalog version with two active SKUs and one retired SKU."""

    active_version_id, retired_version_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        async with session.begin():
            session.add(CatalogVersion(id=retired_version_id, organization_id=organization_id, version_identifier="v0", status=CatalogStatus.RETIRED.value))
            session.add(CatalogVersion(id=active_version_id, organization_id=organization_id, version_identifier="v1", status=CatalogStatus.ACTIVE.value))
            await session.flush()
            session.add(Sku(id=uuid.uuid4(), organization_id=organization_id, catalog_version_id=active_version_id, source_sku_id=uuid.uuid4(), sku_code="B_SKU", name="B Sku", is_active=True))
            session.add(Sku(id=uuid.uuid4(), organization_id=organization_id, catalog_version_id=active_version_id, source_sku_id=uuid.uuid4(), sku_code="A_SKU", name="A Sku", is_active=True))
            session.add(Sku(id=uuid.uuid4(), organization_id=organization_id, catalog_version_id=active_version_id, source_sku_id=uuid.uuid4(), sku_code="RETIRED_SKU", name="Retired Sku", is_active=False))
    return active_version_id, retired_version_id, organization_id


@pytest.mark.asyncio
async def test_get_active_catalog_version_returns_only_the_active_one(organization):
    """Return the one active version, not a draft or retired sibling version."""

    active_version_id, _retired_version_id, organization_id = await _seed_catalog(organization)
    repository = PostgresSkuRepository(get_session_factory())

    record = await repository.get_active_catalog_version(organization_id)

    assert record is not None
    assert record.id == active_version_id
    assert record.version_identifier == "v1"
    assert record.status == "active"


@pytest.mark.asyncio
async def test_get_active_catalog_version_returns_none_for_unknown_organization(organization):
    """Return None rather than raising when the tenant has no active catalog version."""

    repository = PostgresSkuRepository(get_session_factory())

    record = await repository.get_active_catalog_version(uuid.uuid4())

    assert record is None


@pytest.mark.asyncio
async def test_list_active_skus_excludes_inactive_rows_and_orders_by_code(organization):
    """Return only active SKUs, deterministically ordered by ascending sku_code."""

    active_version_id, _retired_version_id, organization_id = await _seed_catalog(organization)
    repository = PostgresSkuRepository(get_session_factory())

    records = await repository.list_active_skus(organization_id, active_version_id)

    assert [record.sku_code for record in records] == ["A_SKU", "B_SKU"]
    assert all(record.is_active for record in records)
    assert all(record.catalog_version_id == active_version_id for record in records)


@pytest.mark.asyncio
async def test_list_active_skus_is_tenant_scoped(organization):
    """Never return one tenant's catalog rows for another tenant's query."""

    active_version_id, _retired_version_id, organization_id = await _seed_catalog(organization)
    repository = PostgresSkuRepository(get_session_factory())

    records = await repository.list_active_skus(uuid.uuid4(), active_version_id)

    assert records == ()
