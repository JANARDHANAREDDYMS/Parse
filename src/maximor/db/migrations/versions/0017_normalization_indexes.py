"""Align normalization and projection indexes with ORM metadata."""
from alembic import op

revision = "0017_normalization_indexes"
down_revision = "0016_normalization_stage5c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, table, columns in (
        ("ix_normalization_runs_document_id", "normalization_runs", ["document_id"]),
        ("ix_normalization_runs_preprocessing_run_id", "normalization_runs", ["preprocessing_run_id"]),
        ("ix_normalization_runs_term_applicability_run_id", "normalization_runs", ["term_applicability_run_id"]),
        ("ix_normalization_runs_processing_job_id", "normalization_runs", ["processing_job_id"]),
        ("ix_normalized_line_items_normalization_run_id", "normalized_line_items", ["normalization_run_id"]),
        ("ix_normalized_line_items_organization_id", "normalized_line_items", ["organization_id"]),
        ("ix_normalization_issues_normalization_run_id", "normalization_issues", ["normalization_run_id"]),
        ("ix_normalization_issues_organization_id", "normalization_issues", ["organization_id"]),
        ("ix_normalization_semantic_findings_normalization_run_id", "normalization_semantic_findings", ["normalization_run_id"]),
        ("ix_normalization_semantic_findings_organization_id", "normalization_semantic_findings", ["organization_id"]),
        ("ix_sku_mapping_decisions_sku_mapping_run_id", "sku_mapping_decisions", ["sku_mapping_run_id"]),
    ):
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table in (
        ("ix_sku_mapping_decisions_sku_mapping_run_id", "sku_mapping_decisions"),
        ("ix_normalization_semantic_findings_organization_id", "normalization_semantic_findings"),
        ("ix_normalization_semantic_findings_normalization_run_id", "normalization_semantic_findings"),
        ("ix_normalization_issues_organization_id", "normalization_issues"),
        ("ix_normalization_issues_normalization_run_id", "normalization_issues"),
        ("ix_normalized_line_items_organization_id", "normalized_line_items"),
        ("ix_normalized_line_items_normalization_run_id", "normalized_line_items"),
        ("ix_normalization_runs_processing_job_id", "normalization_runs"),
        ("ix_normalization_runs_term_applicability_run_id", "normalization_runs"),
        ("ix_normalization_runs_preprocessing_run_id", "normalization_runs"),
        ("ix_normalization_runs_document_id", "normalization_runs"),
    ):
        op.drop_index(name, table_name=table)
