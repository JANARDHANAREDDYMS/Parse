"""Link analysis jobs to a completed preprocessing run exactly once."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0006_analysis_job_link"
down_revision = "0005_analysis_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add trusted preprocessing linkage and analysis-job scheduling uniqueness."""
    op.add_column("processing_jobs", sa.Column("preprocessing_run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_jobs_preproc_run", "processing_jobs", "document_processing_runs", ["preprocessing_run_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_processing_jobs_preprocessing_run_id", "processing_jobs", ["preprocessing_run_id"])
    op.create_index("uq_processing_jobs_analysis_preprocessing_run", "processing_jobs", ["preprocessing_run_id"], unique=True, postgresql_where=sa.text("job_type = 'document_analysis'"))


def downgrade() -> None:
    """Remove analysis scheduling linkage."""
    op.drop_index("uq_processing_jobs_analysis_preprocessing_run", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_preprocessing_run_id", table_name="processing_jobs")
    op.drop_constraint("fk_jobs_preproc_run", "processing_jobs", type_="foreignkey")
    op.drop_column("processing_jobs", "preprocessing_run_id")
