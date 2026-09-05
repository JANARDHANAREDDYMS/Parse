import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from maximor.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from maximor.db.models.statuses import DocumentStatus


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "id", name="uq_documents_organization_id_id"
        ),
        UniqueConstraint(
            "organization_id",
            "storage_key",
            name="uq_documents_organization_storage_key",
        ),
        UniqueConstraint(
            "organization_id",
            "sha256_checksum",
            name="uq_documents_organization_checksum",
        ),
        CheckConstraint("original_filename <> ''", name="filename_not_empty"),
        CheckConstraint(
            "storage_key <> '' AND left(storage_key, 1) <> '/'",
            name="storage_key_relative",
        ),
        CheckConstraint(
            "sha256_checksum ~ '^[0-9a-f]{64}$'", name="sha256_checksum_format"
        ),
        CheckConstraint("file_size IS NULL OR file_size >= 0", name="file_size_valid"),
        CheckConstraint(
            "status IN ('uploaded', 'processing', 'completed', 'failed', "
            "'review_required')",
            name="status_allowed",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(
        String(32), default=DocumentStatus.UPLOADED.value, nullable=False
    )

