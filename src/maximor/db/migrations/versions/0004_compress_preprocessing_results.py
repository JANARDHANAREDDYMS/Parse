"""Add lossless-compression metadata for canonical preprocessing artifacts."""

from alembic import op
import sqlalchemy as sa


revision = "0004_result_compression"
down_revision = "0003_preprocessing_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Allow new results to record stored and canonical-content integrity."""

    op.add_column(
        "document_processing_runs",
        sa.Column("result_content_sha256_checksum", sa.String(64), nullable=True),
    )
    op.add_column(
        "document_processing_runs",
        sa.Column("result_compression", sa.String(16), nullable=True),
    )
    op.add_column(
        "document_processing_runs",
        sa.Column("result_uncompressed_size", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "document_processing_runs",
        sa.Column("result_compressed_size", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_document_processing_runs_result_compression_allowed",
        "document_processing_runs",
        "result_compression IS NULL OR result_compression = 'gzip'",
    )


def downgrade() -> None:
    """Remove canonical-result compression metadata."""

    op.drop_constraint(
        "ck_document_processing_runs_result_compression_allowed",
        "document_processing_runs",
        type_="check",
    )
    op.drop_column("document_processing_runs", "result_compressed_size")
    op.drop_column("document_processing_runs", "result_uncompressed_size")
    op.drop_column("document_processing_runs", "result_compression")
    op.drop_column("document_processing_runs", "result_content_sha256_checksum")
