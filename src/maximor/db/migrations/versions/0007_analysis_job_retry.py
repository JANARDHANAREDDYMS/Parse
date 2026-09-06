"""Permit a later analysis attempt after a terminal historical job."""

from alembic import op
import sqlalchemy as sa


revision = "0007_analysis_job_retry"
down_revision = "0006_analysis_job_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Constrain only concurrent active analysis jobs for one preprocessing run."""
    op.drop_index("uq_processing_jobs_analysis_preprocessing_run", table_name="processing_jobs")
    op.create_index("uq_processing_jobs_analysis_preprocessing_run", "processing_jobs", ["preprocessing_run_id"], unique=True, postgresql_where=sa.text("job_type = 'document_analysis' AND status IN ('queued', 'running')"))


def downgrade() -> None:
    """Restore the original one-job-per-preprocessing-run scheduling invariant."""
    op.drop_index("uq_processing_jobs_analysis_preprocessing_run", table_name="processing_jobs")
    op.create_index("uq_processing_jobs_analysis_preprocessing_run", "processing_jobs", ["preprocessing_run_id"], unique=True, postgresql_where=sa.text("job_type = 'document_analysis'"))
