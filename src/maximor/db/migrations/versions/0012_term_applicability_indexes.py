"""Add indexes declared by the term-applicability ORM models."""

from alembic import op


revision = "0012_term_applicability_indexes"
down_revision = "0011_term_applicability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add tenant/run lookup indexes for candidate applicability links."""
    op.create_index(
        "ix_document_global_term_candidates_analysis_run_id",
        "document_global_term_candidates",
        ["analysis_run_id"],
    )
    op.create_index(
        "ix_document_global_term_candidates_organization_id",
        "document_global_term_candidates",
        ["organization_id"],
    )


def downgrade() -> None:
    """Remove candidate applicability lookup indexes."""
    op.drop_index(
        "ix_document_global_term_candidates_organization_id",
        table_name="document_global_term_candidates",
    )
    op.drop_index(
        "ix_document_global_term_candidates_analysis_run_id",
        table_name="document_global_term_candidates",
    )
