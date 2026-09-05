import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maximor.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Sku(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "skus"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "catalog_version_id"],
            ["catalog_versions.organization_id", "catalog_versions.id"],
            name="fk_skus_tenant_catalog_version",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "catalog_version_id", "sku_code", name="uq_skus_catalog_version_sku_code"
        ),
        UniqueConstraint(
            "catalog_version_id",
            "source_sku_id",
            name="uq_skus_catalog_version_source_sku_id",
        ),
        CheckConstraint("sku_code <> ''", name="sku_code_not_empty"),
        CheckConstraint("name <> ''", name="name_not_empty"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    source_sku_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    sku_code: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    parsing_instructions: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    source_attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

