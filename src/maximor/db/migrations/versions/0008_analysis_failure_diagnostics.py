"""Persist bounded document-analysis diagnostics and complete token accounting."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0008_analysis_diagnostics"
down_revision = "0007_analysis_job_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add value-free failure diagnostics and separate cache-token fields."""

    op.add_column("document_analysis_runs", sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=True))
    op.add_column("document_analysis_runs", sa.Column("cache_read_input_tokens", sa.Integer(), nullable=True))
    op.add_column("document_analysis_runs", sa.Column("model_usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("document_analysis_runs", sa.Column("runtime_diagnostics", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("document_analysis_runs", sa.Column("correction_attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint("correction_attempt_count_nonnegative", "document_analysis_runs", "correction_attempt_count >= 0")


def downgrade() -> None:
    """Remove the bounded diagnostic columns."""

    op.drop_constraint("correction_attempt_count_nonnegative", "document_analysis_runs", type_="check")
    op.drop_column("document_analysis_runs", "correction_attempt_count")
    op.drop_column("document_analysis_runs", "runtime_diagnostics")
    op.drop_column("document_analysis_runs", "model_usage")
    op.drop_column("document_analysis_runs", "cache_read_input_tokens")
    op.drop_column("document_analysis_runs", "cache_creation_input_tokens")
