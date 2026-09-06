"""Persist raw global terms and candidate applicability links."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0011_term_applicability"
down_revision = "0010_sku_mapping_job_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create tenant-scoped term projections without normalizing raw values."""
    op.create_table(
        "document_global_terms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("analysis_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_analysis_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_term_id", sa.String(128), nullable=False),
        sa.Column("raw_name", sa.Text(), nullable=False),
        sa.Column("raw_value", sa.Text()),
        sa.Column("applicability_scope", sa.String(16), nullable=False),
        sa.Column("source_order", sa.Integer(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("analysis_run_id", "external_term_id", name="uq_analysis_global_terms_external_id"),
        sa.CheckConstraint("applicability_scope IN ('document', 'candidate', 'unknown')", name="ck_global_terms_scope_allowed"),
    )
    op.create_index("ix_document_global_terms_analysis_run_id", "document_global_terms", ["analysis_run_id"])
    op.create_index("ix_document_global_terms_organization_id", "document_global_terms", ["organization_id"])

    op.create_table(
        "document_global_term_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("global_term_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_global_terms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_candidate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_product_candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("analysis_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_analysis_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_external_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("global_term_id", "product_candidate_id", name="uq_global_term_candidate_link"),
        sa.ForeignKeyConstraint(
            ["organization_id", "analysis_run_id", "product_candidate_id"],
            ["document_product_candidates.organization_id", "document_product_candidates.analysis_run_id", "document_product_candidates.id"],
            name="fk_global_term_candidates_tenant_candidate", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_document_global_term_candidates_global_term_id", "document_global_term_candidates", ["global_term_id"])
    op.create_index("ix_document_global_term_candidates_product_candidate_id", "document_global_term_candidates", ["product_candidate_id"])


def downgrade() -> None:
    """Remove term projections and applicability links."""
    op.drop_index("ix_document_global_term_candidates_product_candidate_id", table_name="document_global_term_candidates")
    op.drop_index("ix_document_global_term_candidates_global_term_id", table_name="document_global_term_candidates")
    op.drop_table("document_global_term_candidates")
    op.drop_index("ix_document_global_terms_organization_id", table_name="document_global_terms")
    op.drop_index("ix_document_global_terms_analysis_run_id", table_name="document_global_terms")
    op.drop_table("document_global_terms")
