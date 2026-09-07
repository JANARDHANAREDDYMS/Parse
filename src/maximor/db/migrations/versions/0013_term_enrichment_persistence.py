"""Persist combined term triage and commercial-enrichment results."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013_term_enrichment_persistence"
down_revision = "0012_term_applicability_indexes"
branch_labels = None
depends_on = None

def upgrade() -> None:
    """Create enrichment runs and bounded searchable projections."""
    # The foundation constraint predates term-applicability jobs and treated
    # every non-SKU job as unlinked. Extend it before creating linked jobs.
    op.drop_constraint("sku_mapping_link_required", "processing_jobs", type_="check")
    op.create_check_constraint(
        "sku_mapping_link_required",
        "processing_jobs",
        "(job_type = 'sku_mapping' AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NOT NULL) "
        "OR (job_type = 'term_applicability' AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NULL) "
        "OR (job_type NOT IN ('sku_mapping', 'term_applicability') AND analysis_run_id IS NULL AND document_product_candidate_id IS NULL)",
    )
    op.create_check_constraint(
        "term_applicability_link_required",
        "processing_jobs",
        "(job_type = 'term_applicability' AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NULL) OR job_type <> 'term_applicability'",
    )
    op.create_index(
        "uq_processing_jobs_term_applicability_analysis",
        "processing_jobs",
        ["analysis_run_id"],
        unique=True,
        postgresql_where=sa.text("job_type = 'term_applicability' AND status IN ('queued','running')"),
    )
    op.create_table("term_applicability_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False), sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False), sa.Column("analysis_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("document_analysis_runs.id", ondelete="RESTRICT"), nullable=False), sa.Column("processing_job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("processing_jobs.id", ondelete="SET NULL")), sa.Column("attempt_number", sa.Integer(), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("schema_version", sa.String(50), nullable=False), sa.Column("prompt_version", sa.String(100), nullable=False), sa.Column("skill_version", sa.String(100), nullable=False), sa.Column("agent_version", sa.String(100), nullable=False), sa.Column("model", sa.String(100), nullable=False), sa.Column("runtime_diagnostics", postgresql.JSONB()), sa.Column("canonical_result_storage_key", sa.Text()), sa.Column("compressed_sha256_checksum", sa.String(64)), sa.Column("content_sha256_checksum", sa.String(64)), sa.Column("compressed_size", sa.Integer()), sa.Column("uncompressed_size", sa.Integer()), sa.Column("started_at", sa.DateTime(timezone=True), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True)), sa.Column("error_code", sa.String(100)), sa.Column("error_message", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.UniqueConstraint("analysis_run_id", "attempt_number", name="uq_term_applicability_run_attempt"), sa.CheckConstraint("status IN ('running','completed','failed')", name="term_applicability_status_allowed"), sa.CheckConstraint("status <> 'completed' OR canonical_result_storage_key IS NOT NULL", name="term_applicability_completed_artifact_required"))
    for name, cols, unique_cols, unique_name in [("term_triage_decisions", [("term_id",sa.String(128)),("disposition",sa.String(64)),("rationale",sa.Text())], ["run_id", "term_id"], "uq_term_triage_decision_term"), ("term_applicability_decisions",[("term_id",sa.String(128)),("disposition",sa.String(64)),("applicability_scope",sa.String(32)),("candidate_ids",postgresql.JSONB()),("evidence",postgresql.JSONB())], ["run_id", "term_id"], "uq_term_applicability_decision_term"), ("candidate_commercial_fact_coverages",[("candidate_id",sa.String(128)),("expected_fields",postgresql.JSONB()),("extracted_fields",postgresql.JSONB()),("unresolved_fields",postgresql.JSONB()),("evidence",postgresql.JSONB())], ["run_id", "candidate_id"], "uq_candidate_fact_coverage"), ("candidate_commercial_facts",[("candidate_id",sa.String(128)),("fact_id",sa.String(200)),("field",sa.String(64)),("raw_value",sa.Text()),("raw_period_label",sa.String(100)),("evidence",postgresql.JSONB())], ["run_id", "fact_id"], "uq_candidate_commercial_fact")]:
        cols += [("id",postgresql.UUID(as_uuid=True)),("run_id",postgresql.UUID(as_uuid=True)),("organization_id",postgresql.UUID(as_uuid=True)),("source_order",sa.Integer())]
        optional = {"rationale", "raw_period_label"}
        columns=[sa.Column(c,t,primary_key=(c=="id"),nullable=(c not in optional)) for c,t in cols]
        columns.extend([sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)])
        op.create_table(name,*columns,sa.ForeignKeyConstraint(["run_id"],["term_applicability_runs.id"],ondelete="CASCADE"),sa.UniqueConstraint(*unique_cols, name=unique_name))
    op.create_table("term_applicability_evidence_references", sa.Column("id",postgresql.UUID(as_uuid=True),primary_key=True), sa.Column("run_id",postgresql.UUID(as_uuid=True),sa.ForeignKey("term_applicability_runs.id",ondelete="CASCADE"),nullable=False), sa.Column("organization_id",postgresql.UUID(as_uuid=True),nullable=False), sa.Column("owner_type",sa.String(64),nullable=False), sa.Column("owner_id",sa.String(200),nullable=False), sa.Column("preprocessing_run_id",postgresql.UUID(as_uuid=True),sa.ForeignKey("document_processing_runs.id",ondelete="RESTRICT"),nullable=False), sa.Column("page_number",sa.Integer(),nullable=False), sa.Column("external_block_id",sa.String(128)), sa.Column("external_table_id",sa.String(128)), sa.Column("representation",sa.String(32),nullable=False), sa.Column("extraction_source",sa.String(32)), sa.Column("bounding_box",postgresql.JSONB()), sa.Column("source_order",sa.Integer(),nullable=False), sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False), sa.Column("updated_at",sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False), sa.UniqueConstraint("run_id", "owner_type", "owner_id", "source_order", name="uq_term_applicability_evidence_order"), sa.CheckConstraint("num_nonnulls(external_block_id, external_table_id) = 1", name="term_applicability_evidence_one_target"))

def downgrade() -> None:
    """Remove enrichment projections."""
    op.drop_table("term_applicability_evidence_references")
    for name in ("candidate_commercial_facts","candidate_commercial_fact_coverages","term_applicability_decisions","term_triage_decisions"): op.drop_table(name)
    op.drop_table("term_applicability_runs")
    op.drop_index("uq_processing_jobs_term_applicability_analysis", table_name="processing_jobs")
    op.drop_constraint("term_applicability_link_required", "processing_jobs", type_="check")
    op.drop_constraint("sku_mapping_link_required", "processing_jobs", type_="check")
    op.create_check_constraint(
        "sku_mapping_link_required", "processing_jobs",
        "(job_type = 'sku_mapping' AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NOT NULL) OR (job_type <> 'sku_mapping' AND analysis_run_id IS NULL AND document_product_candidate_id IS NULL)",
    )
