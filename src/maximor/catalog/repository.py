from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maximor.catalog.schemas import CatalogSkuInput
from maximor.db.models import CatalogVersion, Organization, Sku
from maximor.db.models.statuses import CatalogStatus


@dataclass(frozen=True)
class CatalogLoadResult:
    organization_id: str
    catalog_version_id: str
    organization_count: int
    catalog_version_count: int
    sku_count: int
    source_checksum: str


class CatalogIngestionRepository:
    """Repository for authoritative, tenant-scoped SKU catalog administration."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_catalog(
        self,
        *,
        organization_slug: str,
        organization_name: str,
        version_identifier: str,
        catalog_status: CatalogStatus,
        source_checksum: str,
        records: list[CatalogSkuInput],
    ) -> CatalogLoadResult:
        organization_id = await self._upsert_organization(
            slug=organization_slug, name=organization_name
        )
        catalog_version_id = await self._upsert_catalog_version(
            organization_id=organization_id,
            version_identifier=version_identifier,
            catalog_status=catalog_status,
            source_checksum=source_checksum,
        )
        await self._synchronize_skus(
            organization_id=organization_id,
            catalog_version_id=catalog_version_id,
            records=records,
        )

        organization_count = await self._session.scalar(
            select(func.count()).select_from(Organization)
        )
        catalog_version_count = await self._session.scalar(
            select(func.count())
            .select_from(CatalogVersion)
            .where(CatalogVersion.organization_id == organization_id)
        )
        sku_count = await self._session.scalar(
            select(func.count())
            .select_from(Sku)
            .where(
                Sku.organization_id == organization_id,
                Sku.catalog_version_id == catalog_version_id,
            )
        )

        return CatalogLoadResult(
            organization_id=str(organization_id),
            catalog_version_id=str(catalog_version_id),
            organization_count=organization_count or 0,
            catalog_version_count=catalog_version_count or 0,
            sku_count=sku_count or 0,
            source_checksum=source_checksum,
        )

    async def _upsert_organization(self, *, slug: str, name: str):
        statement = (
            insert(Organization)
            .values(slug=slug, name=name)
            .on_conflict_do_update(
                constraint="uq_organizations_slug",
                set_={"name": name, "updated_at": func.now()},
            )
            .returning(Organization.id)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def _upsert_catalog_version(
        self,
        *,
        organization_id,
        version_identifier: str,
        catalog_status: CatalogStatus,
        source_checksum: str,
    ):
        activated_at = datetime.now(UTC) if catalog_status is CatalogStatus.ACTIVE else None
        if catalog_status is CatalogStatus.ACTIVE:
            updated_activated_at = func.coalesce(
                CatalogVersion.activated_at, activated_at
            )
        elif catalog_status is CatalogStatus.RETIRED:
            updated_activated_at = CatalogVersion.activated_at
        else:
            updated_activated_at = None

        if catalog_status is CatalogStatus.ACTIVE:
            await self._session.execute(
                update(CatalogVersion)
                .where(
                    CatalogVersion.organization_id == organization_id,
                    CatalogVersion.version_identifier != version_identifier,
                    CatalogVersion.status == CatalogStatus.ACTIVE.value,
                )
                .values(status=CatalogStatus.RETIRED.value, updated_at=func.now())
            )

        statement = (
            insert(CatalogVersion)
            .values(
                organization_id=organization_id,
                version_identifier=version_identifier,
                status=catalog_status.value,
                source_checksum=source_checksum,
                activated_at=activated_at,
            )
            .on_conflict_do_update(
                constraint="uq_catalog_versions_organization_version",
                set_={
                    "status": catalog_status.value,
                    "source_checksum": source_checksum,
                    "activated_at": updated_activated_at,
                    "updated_at": func.now(),
                },
            )
            .returning(CatalogVersion.id)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def _synchronize_skus(
        self, *, organization_id, catalog_version_id, records: list[CatalogSkuInput]
    ) -> None:
        sku_codes = [record.sku_code for record in records]
        await self._session.execute(
            update(Sku)
            .where(
                Sku.organization_id == organization_id,
                Sku.catalog_version_id == catalog_version_id,
                Sku.sku_code.not_in(sku_codes),
            )
            .values(is_active=False, updated_at=func.now())
        )

        for record in records:
            statement = (
                insert(Sku)
                .values(
                    organization_id=organization_id,
                    catalog_version_id=catalog_version_id,
                    source_sku_id=record.id,
                    sku_code=record.sku_code,
                    name=record.name,
                    description=record.description,
                    parsing_instructions=record.parsing_instructions,
                    aliases=[],
                    source_attributes={"usage_count": record.usage_count},
                    is_active=True,
                )
                .on_conflict_do_update(
                    constraint="uq_skus_catalog_version_sku_code",
                    set_={
                        "source_sku_id": record.id,
                        "name": record.name,
                        "description": record.description,
                        "parsing_instructions": record.parsing_instructions,
                        "aliases": [],
                        "source_attributes": {"usage_count": record.usage_count},
                        "is_active": True,
                        "updated_at": func.now(),
                    },
                )
            )
            await self._session.execute(statement)
