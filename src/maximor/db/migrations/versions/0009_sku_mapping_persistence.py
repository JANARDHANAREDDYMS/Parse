"""Persist canonical SKU-mapping results and searchable projections."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0009_sku_mapping_persistence"
down_revision = "0008_analysis_diagnostics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create SKU-mapping run, decision projection, and evidence-reference tables."""
    op.create_table(
        "sku_mapping_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("analysis_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_analysis_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("document_product_candidate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_product_candidates.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("processing_job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("processing_jobs.id", ondelete="SET NULL")),
        sa.Column("catalog_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("catalog_versions.id", ondelete="RESTRICT")),
        sa.Column("supersedes_mapping_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sku_mapping_runs.id", ondelete="RESTRICT")),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.String(50), nullable=False),
        sa.Column("document_analysis_schema_version", sa.String(50), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("skill_version", sa.String(100), nullable=False),
        sa.Column("agent_version", sa.String(100), nullable=False),
        sa.Column("retriever_version", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("cache_creation_input_tokens", sa.Integer()),
        sa.Column("cache_read_input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("model_usage", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("tool_call_count", sa.Integer()),
        sa.Column("turn_count", sa.Integer()),
        sa.Column("reported_cost_usd", sa.Numeric(12, 6)),
        sa.Column("terminal_reason", sa.String(100)),
        sa.Column("runtime_diagnostics", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("correction_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("validation_status", sa.String(32), nullable=False, server_default="pending"),
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
        sa.ForeignKeyConstraint(["organization_id", "document_id"], ["documents.organization_id", "documents.id"], name="fk_sku_mapping_runs_tenant_document", ondelete="CASCADE"),
        sa.UniqueConstraint("document_product_candidate_id", "attempt_number", name="uq_sku_mapping_runs_candidate_attempt"),
        sa.CheckConstraint("attempt_number >= 1", name="ck_sku_mapping_runs_attempt_number_positive"),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_sku_mapping_runs_status_allowed"),
        sa.CheckConstraint("status <> 'completed' OR (canonical_result_storage_key IS NOT NULL AND completed_at IS NOT NULL AND validation_status = 'validated' AND catalog_version_id IS NOT NULL)", name="ck_sku_mapping_runs_completed_result_required"),
        sa.CheckConstraint("status <> 'failed' OR canonical_result_storage_key IS NULL", name="ck_sku_mapping_runs_failed_result_absent"),
        sa.CheckConstraint("compression_method IS NULL OR compression_method = 'gzip'", name="ck_sku_mapping_runs_compression_allowed"),
        sa.CheckConstraint("correction_attempt_count >= 0", name="ck_sku_mapping_runs_correction_attempt_count_nonnegative"),
    )
    for column in ("organization_id", "document_id", "analysis_run_id", "document_product_candidate_id", "processing_job_id", "catalog_version_id", "supersedes_mapping_run_id"):
        op.create_index(f"ix_sku_mapping_runs_{column}", "sku_mapping_runs", [column])

    op.create_table(
        "sku_mapping_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sku_mapping_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sku_mapping_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_product_candidate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_product_candidates.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("sku_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("skus.id", ondelete="RESTRICT")),
        sa.Column("sku_code", sa.String(255)),
        sa.Column("sku_name", sa.String(512)),
        sa.Column("considered_sku_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("rationale", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("sku_mapping_run_id", name="uq_sku_mapping_decisions_run"),
        sa.CheckConstraint("outcome IN ('match', 'no_match', 'ambiguous')", name="ck_sku_mapping_decisions_outcome_allowed"),
        sa.CheckConstraint(
            "(outcome = 'match' AND sku_id IS NOT NULL AND sku_code IS NOT NULL AND sku_name IS NOT NULL) "
            "OR (outcome <> 'match' AND sku_id IS NULL AND sku_code IS NULL AND sku_name IS NULL)",
            name="ck_sku_mapping_decisions_match_fields_consistent",
        ),
    )
    op.create_index("ix_sku_mapping_decisions_organization_id", "sku_mapping_decisions", ["organization_id"])
    op.create_index("ix_sku_mapping_decisions_document_product_candidate_id", "sku_mapping_decisions", ["document_product_candidate_id"])
    op.create_index("ix_sku_mapping_decisions_sku_id", "sku_mapping_decisions", ["sku_id"])

    op.create_table(
        "sku_mapping_evidence_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sku_mapping_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sku_mapping_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
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
        sa.UniqueConstraint("sku_mapping_run_id", "evidence_order", name="uq_sku_mapping_evidence_run_order"),
        sa.CheckConstraint("(document_block_id IS NOT NULL AND document_table_id IS NULL) OR (document_block_id IS NULL AND document_table_id IS NOT NULL)", name="ck_sku_mapping_evidence_references_one_preprocessing_target"),
    )
    for column in ("sku_mapping_run_id", "organization_id", "preprocessing_run_id", "document_block_id", "document_table_id"):
        op.create_index(f"ix_sku_mapping_evidence_references_{column}", "sku_mapping_evidence_references", [column])


def downgrade() -> None:
    """Remove SKU-mapping persistence tables."""
    op.drop_table("sku_mapping_evidence_references")
    op.drop_table("sku_mapping_decisions")
    op.drop_table("sku_mapping_runs")
