import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import typer
from pydantic import TypeAdapter, ValidationError

from maximor.catalog.repository import SkuRepository
from maximor.catalog.schemas import CatalogSkuInput
from maximor.db.models.statuses import CatalogStatus
from maximor.db.session import session_scope


def read_catalog(catalog_path: Path) -> tuple[list[CatalogSkuInput], str]:
    """Read and validate only the explicitly supplied catalog file."""

    raw_catalog = catalog_path.read_bytes()
    checksum = hashlib.sha256(raw_catalog).hexdigest()

    try:
        records = TypeAdapter(list[CatalogSkuInput]).validate_json(raw_catalog)
    except ValidationError as exc:
        raise ValueError(f"Catalog validation failed: {exc}") from exc

    if not records:
        raise ValueError("Catalog must contain at least one SKU")

    sku_codes = [record.sku_code for record in records]
    source_ids = [record.id for record in records]
    if len(sku_codes) != len(set(sku_codes)):
        raise ValueError("Catalog contains duplicate sku_code values")
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("Catalog contains duplicate id values")

    return records, checksum


async def load_catalog(
    *,
    catalog_path: Path,
    organization_slug: str,
    organization_name: str,
    catalog_version: str,
    catalog_status: CatalogStatus,
) -> dict[str, str | int]:
    organization_slug = organization_slug.strip()
    organization_name = organization_name.strip()
    catalog_version = catalog_version.strip()
    if not organization_slug or len(organization_slug) > 100:
        raise ValueError("organization_slug must contain 1 to 100 characters")
    if not organization_name or len(organization_name) > 255:
        raise ValueError("organization_name must contain 1 to 255 characters")
    if not catalog_version or len(catalog_version) > 100:
        raise ValueError("catalog_version must contain 1 to 100 characters")

    records, checksum = read_catalog(catalog_path)

    async with session_scope() as session:
        result = await SkuRepository(session).load_catalog(
            organization_slug=organization_slug,
            organization_name=organization_name,
            version_identifier=catalog_version,
            catalog_status=catalog_status,
            source_checksum=checksum,
            records=records,
        )

    return asdict(result)


def main(
    catalog_path: Path = typer.Option(
        ...,
        "--catalog-path",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Explicit path to one SKU catalog JSON file.",
    ),
    organization_slug: str = typer.Option(
        "demo", "--organization-slug", help="Stable tenant slug."
    ),
    organization_name: str = typer.Option(
        "Maximor Demo Organization", "--organization-name"
    ),
    catalog_version: str = typer.Option(
        "synthetic-v1", "--catalog-version", help="Tenant-scoped version identifier."
    ),
    catalog_status: CatalogStatus = typer.Option(
        CatalogStatus.ACTIVE, "--catalog-status", case_sensitive=False
    ),
) -> None:
    """Idempotently load a validated SKU catalog into PostgreSQL."""

    try:
        result = asyncio.run(
            load_catalog(
                catalog_path=catalog_path,
                organization_slug=organization_slug,
                organization_name=organization_name,
                catalog_version=catalog_version,
                catalog_status=catalog_status,
            )
        )
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    typer.run(main)
