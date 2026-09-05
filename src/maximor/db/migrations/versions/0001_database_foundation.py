"""Create the database foundation and enable pgvector.

Revision ID: 0001_database_foundation
Revises:
Create Date: 2026-09-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_database_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "organizations",
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("name <> ''", name="name_not_empty"),
        sa.CheckConstraint("slug <> ''", name="slug_not_empty"),
        sa.PrimaryKeyConstraint("id", name="pk_organizations"),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
    )

    op.create_table(
        "catalog_versions",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("version_identifier", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_checksum", sa.String(length=64), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_checksum IS NULL OR source_checksum ~ '^[0-9a-f]{64}$'",
            name="source_checksum_format",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'retired')",
            name="status_allowed",
        ),
        sa.CheckConstraint(
            "version_identifier <> ''",
            name="version_identifier_not_empty",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_catalog_versions_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_catalog_versions"),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_catalog_versions_organization_id_id"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "version_identifier",
            name="uq_catalog_versions_organization_version",
        ),
    )
    op.create_index(
        "ix_catalog_versions_organization_id",
        "catalog_versions",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "uq_catalog_versions_one_active_per_org",
        "catalog_versions",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "documents",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("sha256_checksum", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "file_size IS NULL OR file_size >= 0",
            name="file_size_valid",
        ),
        sa.CheckConstraint(
            "original_filename <> ''", name="filename_not_empty"
        ),
        sa.CheckConstraint(
            "sha256_checksum ~ '^[0-9a-f]{64}$'",
            name="sha256_checksum_format",
        ),
        sa.CheckConstraint(
            "status IN ('uploaded', 'processing', 'completed', 'failed', "
            "'review_required')",
            name="status_allowed",
        ),
        sa.CheckConstraint(
            "storage_key <> '' AND left(storage_key, 1) <> '/'",
            name="storage_key_relative",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_documents_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint(
            "organization_id",
            "sha256_checksum",
            name="uq_documents_organization_checksum",
        ),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_documents_organization_id_id"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "storage_key",
            name="uq_documents_organization_storage_key",
        ),
    )
    op.create_index(
        "ix_documents_organization_id", "documents", ["organization_id"], unique=False
    )

    op.create_table(
        "processing_jobs",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_number >= 1", name="attempt_number_positive"
        ),
        sa.CheckConstraint(
            "job_type <> ''", name="job_type_not_empty"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', "
            "'review_required')",
            name="status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_processing_jobs_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["documents.organization_id", "documents.id"],
            name="fk_processing_jobs_tenant_document",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_processing_jobs"),
    )
    op.create_index(
        "ix_processing_jobs_document_id",
        "processing_jobs",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        "ix_processing_jobs_organization_id",
        "processing_jobs",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "skus",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("source_sku_id", sa.Uuid(), nullable=False),
        sa.Column("sku_code", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parsing_instructions", sa.Text(), nullable=True),
        sa.Column(
            "aliases",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "source_attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("name <> ''", name="name_not_empty"),
        sa.CheckConstraint("sku_code <> ''", name="sku_code_not_empty"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_skus_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "catalog_version_id"],
            ["catalog_versions.organization_id", "catalog_versions.id"],
            name="fk_skus_tenant_catalog_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_skus"),
        sa.UniqueConstraint(
            "catalog_version_id",
            "sku_code",
            name="uq_skus_catalog_version_sku_code",
        ),
        sa.UniqueConstraint(
            "catalog_version_id",
            "source_sku_id",
            name="uq_skus_catalog_version_source_sku_id",
        ),
    )
    op.create_index(
        "ix_skus_catalog_version_id", "skus", ["catalog_version_id"], unique=False
    )
    op.create_index(
        "ix_skus_organization_id", "skus", ["organization_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_skus_organization_id", table_name="skus")
    op.drop_index("ix_skus_catalog_version_id", table_name="skus")
    op.drop_table("skus")
    op.drop_index("ix_processing_jobs_organization_id", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_document_id", table_name="processing_jobs")
    op.drop_table("processing_jobs")
    op.drop_index("ix_documents_organization_id", table_name="documents")
    op.drop_table("documents")
    op.drop_index("uq_catalog_versions_one_active_per_org", table_name="catalog_versions")
    op.drop_index("ix_catalog_versions_organization_id", table_name="catalog_versions")
    op.drop_table("catalog_versions")
    op.drop_table("organizations")
    # The vector extension is intentionally retained because other schemas may use it.
