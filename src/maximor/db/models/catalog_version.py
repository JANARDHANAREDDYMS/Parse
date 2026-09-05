import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from maximor.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from maximor.db.models.statuses import CatalogStatus


class CatalogVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "catalog_versions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "id", name="uq_catalog_versions_organization_id_id"
        ),
        UniqueConstraint(
            "organization_id",
            "version_identifier",
            name="uq_catalog_versions_organization_version",
        ),
        CheckConstraint("version_identifier <> ''", name="version_identifier_not_empty"),
        CheckConstraint(
            "status IN ('draft', 'active', 'retired')", name="status_allowed"
        ),
        CheckConstraint(
            "source_checksum IS NULL OR source_checksum ~ '^[0-9a-f]{64}$'",
            name="source_checksum_format",
        ),
        Index(
            "uq_catalog_versions_one_active_per_org",
            "organization_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_identifier: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=CatalogStatus.DRAFT.value, nullable=False
    )
    source_checksum: Mapped[str | None] = mapped_column(String(64))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

