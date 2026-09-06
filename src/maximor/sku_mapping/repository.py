"""Query the authoritative tenant-scoped SKU catalog for retrieval.

This is the production read path over the existing `catalog_versions` and
`skus` tables. It performs parameterized SQLAlchemy queries only, exposes
neither SQL nor sessions to callers, and never mutates catalog state — writes
remain owned exclusively by `maximor.catalog.repository.CatalogIngestionRepository`.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maximor.db.models import CatalogVersion, Sku
from maximor.db.models.statuses import CatalogStatus
from maximor.sku_mapping.schemas import CatalogVersionRecord, SkuRecord


class PostgresSkuRepository:
    """Implement `SkuRepository` as read-only, tenant-scoped Postgres queries."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Receive the application session factory; callers never receive a session."""

        self._sessions = sessions

    async def get_active_catalog_version(self, organization_id: uuid.UUID) -> CatalogVersionRecord | None:
        """Return the one active catalog version for a tenant, or None."""

        async with self._sessions() as session:
            row = await session.scalar(
                select(CatalogVersion).where(
                    CatalogVersion.organization_id == organization_id,
                    CatalogVersion.status == CatalogStatus.ACTIVE.value,
                )
            )
        if row is None:
            return None
        return CatalogVersionRecord(
            id=row.id,
            organization_id=row.organization_id,
            version_identifier=row.version_identifier,
            status=row.status,
        )

    async def list_active_skus(
        self, organization_id: uuid.UUID, catalog_version_id: uuid.UUID
    ) -> tuple[SkuRecord, ...]:
        """Return every active SKU in one tenant-scoped catalog version, ordered by code."""

        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(Sku)
                    .where(
                        Sku.organization_id == organization_id,
                        Sku.catalog_version_id == catalog_version_id,
                        Sku.is_active.is_(True),
                    )
                    .order_by(Sku.sku_code)
                )
            ).all()
        return tuple(
            SkuRecord(
                id=row.id,
                source_sku_id=row.source_sku_id,
                organization_id=row.organization_id,
                catalog_version_id=row.catalog_version_id,
                sku_code=row.sku_code,
                name=row.name,
                description=row.description,
                parsing_instructions=row.parsing_instructions,
                aliases=tuple(row.aliases or ()),
                is_active=row.is_active,
            )
            for row in rows
        )
