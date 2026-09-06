"""Persist canonical document-analysis results and searchable projections."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_analysis_persistence"
down_revision = "0004_result_compression"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create analysis workflow, projection, and evidence-reference tables."""
    op.create_table(
        "document_analysis_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("preprocessing_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_processing_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("processing_job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("processing_jobs.id", ondelete="SET NULL")),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.String(50), nullable=False),
        sa.Column("preprocessing_schema_version", sa.String(50), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("skill_version", sa.String(100), nullable=False),
        sa.Column("agent_version", sa.String(100), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("tool_call_count", sa.Integer()),
        sa.Column("turn_count", sa.Integer()),
        sa.Column("reported_cost_usd", sa.Numeric(12, 6)),
        sa.Column("terminal_reason", sa.String(100)),
        sa.Column("validation_status", sa.String(32), nullable=False),
        sa.Column("canonical_result_storage_key", sa.Text()),
        sa.Column("compression_method", sa.String(16)),
        sa.Column("compressed_sha256_checksum", sa.String(64)),
        sa.Column("content_sha256_checksum", sa.String(64)),
        sa.Column("compressed_size", sa.BigInteger()),
        sa.Column("uncompressed_size", sa.BigInteger()),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "document_id"], ["documents.organization_id", "documents.id"], name="fk_document_analysis_runs_tenant_document", ondelete="CASCADE"),
        sa.UniqueConstraint("preprocessing_run_id", "attempt_number", name="uq_analysis_runs_preprocessing_attempt"),
        sa.CheckConstraint("attempt_number >= 1", name="ck_document_analysis_runs_attempt_number_positive"),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_document_analysis_runs_status_allowed"),
        sa.CheckConstraint("status <> 'completed' OR (canonical_result_storage_key IS NOT NULL AND completed_at IS NOT NULL AND validation_status = 'validated')", name="ck_document_analysis_runs_completed_result_required"),
        sa.CheckConstraint("status <> 'failed' OR canonical_result_storage_key IS NULL", name="ck_document_analysis_runs_failed_result_absent"),
        sa.CheckConstraint("compression_method IS NULL OR compression_method = 'gzip'", name="ck_document_analysis_runs_compression_allowed"),
    )
    op.create_index("ix_document_analysis_runs_organization_id", "document_analysis_runs", ["organization_id"])
    op.create_index("ix_document_analysis_runs_document_id", "document_analysis_runs", ["document_id"])
    op.create_index("ix_document_analysis_runs_preprocessing_run_id", "document_analysis_runs", ["preprocessing_run_id"])
    op.create_index("ix_document_analysis_runs_processing_job_id", "document_analysis_runs", ["processing_job_id"])

    op.create_table(
        "document_product_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("analysis_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_analysis_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_candidate_id", sa.String(128), nullable=False),
        sa.Column("source_order", sa.Integer(), nullable=False),
        sa.Column("raw_name", sa.Text(), nullable=False),
        sa.Column("raw_attributes", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("analysis_run_id", "external_candidate_id", name="uq_analysis_candidates_external_id"),
    )
    op.create_index("ix_document_product_candidates_analysis_run_id", "document_product_candidates", ["analysis_run_id"])
    op.create_index("ix_document_product_candidates_organization_id", "document_product_candidates", ["organization_id"])

    op.create_table(
        "document_commercial_status_assessments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("analysis_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_analysis_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_candidate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_product_candidates.id", ondelete="RESTRICT")),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_assessment_id", sa.String(128), nullable=False),
        sa.Column("commercial_status", sa.String(32), nullable=False),
        sa.Column("raw_rationale", sa.Text()),
        sa.Column("source_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("analysis_run_id", "external_assessment_id", name="uq_analysis_assessments_external_id"),
    )
    op.create_index("ix_document_commercial_status_assessments_analysis_run_id", "document_commercial_status_assessments", ["analysis_run_id"])
    op.create_index("ix_document_commercial_status_assessments_product_candidate_id", "document_commercial_status_assessments", ["product_candidate_id"])
    op.create_index("ix_document_commercial_status_assessments_organization_id", "document_commercial_status_assessments", ["organization_id"])

    op.create_table(
        "document_analysis_evidence_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("analysis_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_analysis_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_type", sa.String(48), nullable=False),
        sa.Column("owner_external_id", sa.String(128), nullable=False),
        sa.Column("preprocessing_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_processing_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("document_block_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_blocks.id", ondelete="RESTRICT")),
        sa.Column("document_table_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_tables.id", ondelete="RESTRICT")),
        sa.Column("external_block_id", sa.String(128)),
        sa.Column("external_table_id", sa.String(128)),
        sa.Column("representation", sa.String(32), nullable=False),
        sa.Column("extraction_source", sa.String(32)),
        sa.Column("x0", sa.Float()), sa.Column("y0", sa.Float()), sa.Column("x1", sa.Float()), sa.Column("y1", sa.Float()),
        sa.Column("evidence_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("analysis_run_id", "owner_type", "owner_external_id", "evidence_order", name="uq_analysis_evidence_owner_order"),
        sa.CheckConstraint("owner_type IN ('document_analysis_result', 'contract_structure', 'pricing_section', 'global_term', 'product_candidate', 'commercial_status_assessment')", name="ck_document_analysis_evidence_references_owner_type_allowed"),
        sa.CheckConstraint("(document_block_id IS NOT NULL AND document_table_id IS NULL) OR (document_block_id IS NULL AND document_table_id IS NOT NULL)", name="ck_document_analysis_evidence_references_one_preprocessing_target"),
    )
    for column in ("analysis_run_id", "organization_id", "preprocessing_run_id", "document_block_id", "document_table_id"):
        op.create_index(f"ix_document_analysis_evidence_references_{column}", "document_analysis_evidence_references", [column])


def downgrade() -> None:
    """Remove document-analysis persistence tables."""
    op.drop_table("document_analysis_evidence_references")
    op.drop_table("document_commercial_status_assessments")
    op.drop_table("document_product_candidates")
    op.drop_table("document_analysis_runs")
