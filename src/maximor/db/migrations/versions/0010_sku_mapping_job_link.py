"""Link SKU-mapping jobs to one analysis run and candidate exactly once.

Adds the composite-unique targets on document_analysis_runs and
document_product_candidates needed for tenant-safe composite foreign keys
from processing_jobs, then the two new nullable linking columns, their
composite FKs, a CHECK requiring both fields set iff job_type='sku_mapping',
and a partial unique index preventing two concurrently active sku_mapping
jobs for the same (analysis_run_id, document_product_candidate_id) — mirrors
0006_analysis_job_link.py's document_analysis chaining pattern, extended for
a compound key since one analysis run has many candidates.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_sku_mapping_job_link"
down_revision = "0009_sku_mapping_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add composite-unique targets, job linking columns, FKs, and scheduling uniqueness."""
    op.create_unique_constraint(
        "uq_document_analysis_runs_tenant_document_id", "document_analysis_runs",
        ["organization_id", "document_id", "id"],
    )
    op.create_unique_constraint(
        "uq_document_product_candidates_tenant_run_id", "document_product_candidates",
        ["organization_id", "analysis_run_id", "id"],
    )

    op.add_column("processing_jobs", sa.Column("analysis_run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("processing_jobs", sa.Column("document_product_candidate_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_processing_jobs_analysis_run_id", "processing_jobs", ["analysis_run_id"])
    op.create_index("ix_processing_jobs_document_product_candidate_id", "processing_jobs", ["document_product_candidate_id"])

    op.create_foreign_key(
        "fk_jobs_tenant_analysis_run", "processing_jobs", "document_analysis_runs",
        ["organization_id", "document_id", "analysis_run_id"],
        ["organization_id", "document_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_jobs_tenant_analysis_candidate", "processing_jobs", "document_product_candidates",
        ["organization_id", "analysis_run_id", "document_product_candidate_id"],
        ["organization_id", "analysis_run_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "sku_mapping_link_required", "processing_jobs",
        "(job_type = 'sku_mapping' AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NOT NULL) "
        "OR (job_type <> 'sku_mapping' AND analysis_run_id IS NULL AND document_product_candidate_id IS NULL)",
    )
    op.create_index(
        "uq_processing_jobs_sku_mapping_analysis_candidate", "processing_jobs",
        ["analysis_run_id", "document_product_candidate_id"],
        unique=True, postgresql_where=sa.text("job_type = 'sku_mapping' AND status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    """Remove SKU-mapping job linking and its supporting composite-unique targets."""
    op.drop_index("uq_processing_jobs_sku_mapping_analysis_candidate", table_name="processing_jobs")
    op.drop_constraint("sku_mapping_link_required", "processing_jobs", type_="check")
    op.drop_constraint("fk_jobs_tenant_analysis_candidate", "processing_jobs", type_="foreignkey")
    op.drop_constraint("fk_jobs_tenant_analysis_run", "processing_jobs", type_="foreignkey")
    op.drop_index("ix_processing_jobs_document_product_candidate_id", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_analysis_run_id", table_name="processing_jobs")
    op.drop_column("processing_jobs", "document_product_candidate_id")
    op.drop_column("processing_jobs", "analysis_run_id")
    op.drop_constraint("uq_document_product_candidates_tenant_run_id", "document_product_candidates", type_="unique")
    op.drop_constraint("uq_document_analysis_runs_tenant_document_id", "document_analysis_runs", type_="unique")
