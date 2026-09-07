"""normalization Stage 5C persistence and queue linkage

Revision ID: 0016_normalization_stage5c
Revises: 0015_term_enrichment_schema_fix
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016_normalization_stage5c"
down_revision = "0015_term_enrichment_schema_fix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("sku_mapping_link_required", "processing_jobs", type_="check")
    op.drop_constraint("term_applicability_link_required", "processing_jobs", type_="check")
    op.create_check_constraint(
        "processing_job_link_required", "processing_jobs",
        "(job_type = 'sku_mapping' AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NOT NULL) "
        "OR (job_type IN ('term_applicability','normalization') AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NULL) "
        "OR (job_type NOT IN ('sku_mapping','term_applicability','normalization') AND analysis_run_id IS NULL AND document_product_candidate_id IS NULL)",
    )
    op.create_check_constraint(
        "term_applicability_link_required", "processing_jobs",
        "(job_type IN ('term_applicability','normalization') AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NULL) OR (job_type NOT IN ('term_applicability','normalization'))",
    )
    op.create_index("uq_processing_jobs_normalization_analysis", "processing_jobs", ["analysis_run_id"], unique=True, postgresql_where=sa.text("job_type = 'normalization' AND status IN ('queued','running')"))
    op.create_table(
        "normalization_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("preprocessing_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_processing_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("analysis_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_analysis_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("term_applicability_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("term_applicability_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("processing_job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("processing_jobs.id", ondelete="SET NULL")),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.String(50), nullable=False),
        sa.Column("finalization_policy_version", sa.String(50), nullable=False),
        sa.Column("semantic_review_versions", postgresql.JSONB()),
        sa.Column("runtime_diagnostics", postgresql.JSONB()),
        sa.Column("canonical_result_storage_key", sa.Text()),
        sa.Column("compression_method", sa.String(16)),
        sa.Column("compressed_sha256_checksum", sa.String(64)),
        sa.Column("content_sha256_checksum", sa.String(64)),
        sa.Column("compressed_size", sa.Integer()), sa.Column("uncompressed_size", sa.Integer()),
        sa.Column("reported_cost_usd", sa.Numeric(12, 6)),
        sa.Column("error_code", sa.String(100)), sa.Column("error_stage", sa.String(100)), sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("analysis_run_id", "term_applicability_run_id", "attempt_number", name="uq_normalization_run_attempt"),
        sa.CheckConstraint("status IN ('running','completed','review_required','failed_validation','failed')", name="normalization_status_allowed"),
        sa.CheckConstraint("status NOT IN ('completed','review_required') OR canonical_result_storage_key IS NOT NULL", name="normalization_artifact_required"),
        sa.ForeignKeyConstraint(["organization_id", "document_id"], ["documents.organization_id", "documents.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_normalization_runs_organization_id", "normalization_runs", ["organization_id"])
    op.create_index("ix_normalization_runs_document_id", "normalization_runs", ["document_id"])
    op.create_index("ix_normalization_runs_preprocessing_run_id", "normalization_runs", ["preprocessing_run_id"])
    op.create_index("ix_normalization_runs_analysis_run_id", "normalization_runs", ["analysis_run_id"])
    op.create_index("ix_normalization_runs_term_applicability_run_id", "normalization_runs", ["term_applicability_run_id"])
    op.create_index("ix_normalization_runs_processing_job_id", "normalization_runs", ["processing_job_id"])
    op.create_table("normalized_line_items", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("normalization_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("normalization_runs.id", ondelete="CASCADE"), nullable=False), sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False), sa.Column("source_candidate_id", sa.String(128), nullable=False), sa.Column("sku_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("skus.id", ondelete="RESTRICT")), sa.Column("sku_code", sa.String(255)), sa.Column("sku_name", sa.String(512)), sa.Column("fields", postgresql.JSONB(), nullable=False), sa.Column("provenance", postgresql.JSONB(), nullable=False), sa.Column("source_order", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.UniqueConstraint("normalization_run_id", "source_candidate_id", name="uq_normalized_line_item_candidate"))
    op.create_index("ix_normalized_line_items_normalization_run_id", "normalized_line_items", ["normalization_run_id"])
    op.create_index("ix_normalized_line_items_organization_id", "normalized_line_items", ["organization_id"])
    op.create_table("normalization_issues", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("normalization_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("normalization_runs.id", ondelete="CASCADE"), nullable=False), sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False), sa.Column("code", sa.String(100), nullable=False), sa.Column("classification", sa.String(64), nullable=False), sa.Column("location", sa.String(200), nullable=False), sa.Column("candidate_id", sa.String(128)), sa.Column("field_name", sa.String(100)), sa.Column("hard", sa.Boolean(), nullable=False), sa.Column("source_order", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.UniqueConstraint("normalization_run_id", "source_order", name="uq_normalization_issue_order"))
    op.create_index("ix_normalization_issues_normalization_run_id", "normalization_issues", ["normalization_run_id"])
    op.create_index("ix_normalization_issues_organization_id", "normalization_issues", ["organization_id"])
    op.create_table("normalization_semantic_findings", sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("normalization_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("normalization_runs.id", ondelete="CASCADE"), nullable=False), sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False), sa.Column("review_item_id", sa.String(128), nullable=False), sa.Column("outcome", sa.String(64), nullable=False), sa.Column("owner", sa.String(64)), sa.Column("candidate_id", sa.String(128)), sa.Column("field_name", sa.String(100)), sa.Column("evidence_ids", postgresql.JSONB(), nullable=False), sa.Column("rationale", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.UniqueConstraint("normalization_run_id", "review_item_id", name="uq_normalization_semantic_item"))
    op.create_index("ix_normalization_semantic_findings_normalization_run_id", "normalization_semantic_findings", ["normalization_run_id"])
    op.create_index("ix_normalization_semantic_findings_organization_id", "normalization_semantic_findings", ["organization_id"])


def downgrade() -> None:
    op.drop_table("normalization_semantic_findings")
    op.drop_table("normalization_issues")
    op.drop_table("normalized_line_items")
    op.drop_index("ix_normalization_runs_processing_job_id", table_name="normalization_runs")
    op.drop_index("ix_normalization_runs_term_applicability_run_id", table_name="normalization_runs")
    op.drop_index("ix_normalization_runs_analysis_run_id", table_name="normalization_runs")
    op.drop_index("ix_normalization_runs_preprocessing_run_id", table_name="normalization_runs")
    op.drop_index("ix_normalization_runs_document_id", table_name="normalization_runs")
    op.drop_index("ix_normalization_runs_organization_id", table_name="normalization_runs")
    op.drop_table("normalization_runs")
    op.drop_index("uq_processing_jobs_normalization_analysis", table_name="processing_jobs")
    op.drop_constraint("processing_job_link_required", "processing_jobs", type_="check")
    op.drop_constraint("term_applicability_link_required", "processing_jobs", type_="check")
    op.create_check_constraint("term_applicability_link_required", "processing_jobs", "(job_type = 'term_applicability' AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NULL) OR job_type <> 'term_applicability'")
    op.create_check_constraint("sku_mapping_link_required", "processing_jobs", "(job_type = 'sku_mapping' AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NOT NULL) OR (job_type = 'term_applicability' AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NULL) OR (job_type NOT IN ('sku_mapping', 'term_applicability') AND analysis_run_id IS NULL AND document_product_candidate_id IS NULL)")
