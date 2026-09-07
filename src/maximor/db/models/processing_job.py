import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from maximor.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from maximor.db.models.statuses import ProcessingJobStatus


class ProcessingJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["documents.organization_id", "documents.id"],
            name="fk_processing_jobs_tenant_document",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "document_id", "analysis_run_id"],
            ["document_analysis_runs.organization_id", "document_analysis_runs.document_id", "document_analysis_runs.id"],
            name="fk_jobs_tenant_analysis_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "analysis_run_id", "document_product_candidate_id"],
            ["document_product_candidates.organization_id", "document_product_candidates.analysis_run_id", "document_product_candidates.id"],
            name="fk_jobs_tenant_analysis_candidate",
            ondelete="RESTRICT",
        ),
        CheckConstraint("job_type <> ''", name="job_type_not_empty"),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', "
            "'review_required')",
            name="status_allowed",
        ),
        CheckConstraint(
            "(job_type = 'sku_mapping' AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NOT NULL) "
            "OR (job_type IN ('term_applicability','normalization') AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NULL) "
            "OR (job_type NOT IN ('sku_mapping', 'term_applicability','normalization') AND analysis_run_id IS NULL AND document_product_candidate_id IS NULL)",
            name="processing_job_link_required",
        ),
        CheckConstraint(
            "(job_type IN ('term_applicability','normalization') AND analysis_run_id IS NOT NULL AND document_product_candidate_id IS NULL) OR (job_type NOT IN ('term_applicability','normalization'))",
            name="term_applicability_link_required",
        ),
        Index(
            "uq_processing_jobs_analysis_preprocessing_run",
            "preprocessing_run_id",
            unique=True,
            postgresql_where=text("job_type = 'document_analysis' AND status IN ('queued', 'running')"),
        ),
        Index("uq_processing_jobs_term_applicability_analysis", "analysis_run_id", unique=True, postgresql_where=text("job_type = 'term_applicability' AND status IN ('queued','running')")),
        Index(
            "uq_processing_jobs_sku_mapping_analysis_candidate",
            "analysis_run_id", "document_product_candidate_id",
            unique=True,
            postgresql_where=text("job_type = 'sku_mapping' AND status IN ('queued', 'running')"),
        ),
        Index("uq_processing_jobs_normalization_analysis", "analysis_run_id", unique=True, postgresql_where=text("job_type = 'normalization' AND status IN ('queued','running')")),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    preprocessing_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_processing_runs.id", ondelete="RESTRICT", name="fk_jobs_preproc_run"), index=True
    )
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    document_product_candidate_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=ProcessingJobStatus.QUEUED.value, nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
