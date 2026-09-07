"""Tenant-safe normalization run state and searchable projections."""
import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, Numeric, String, Text, UniqueConstraint, Index, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from maximor.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NormalizationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Store one immutable normalization attempt and canonical artifact metadata."""
    __tablename__ = "normalization_runs"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "term_applicability_run_id", "attempt_number", name="uq_normalization_run_attempt"),
        CheckConstraint("status IN ('running','completed','review_required','failed_validation','failed')", name="normalization_status_allowed"),
        CheckConstraint("status NOT IN ('completed','review_required') OR canonical_result_storage_key IS NOT NULL", name="normalization_artifact_required"),
        ForeignKeyConstraint(["organization_id", "document_id"], ["documents.organization_id", "documents.id"], name="fk_normalization_runs_organization_id_documents", ondelete="CASCADE"),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    preprocessing_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_processing_runs.id", ondelete="RESTRICT"), nullable=False, index=True)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_analysis_runs.id", ondelete="RESTRICT"), nullable=False, index=True)
    term_applicability_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("term_applicability_runs.id", ondelete="RESTRICT"), nullable=False, index=True)
    processing_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("processing_jobs.id", ondelete="SET NULL"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    finalization_policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    semantic_review_versions: Mapped[dict | None] = mapped_column(JSONB)
    runtime_diagnostics: Mapped[dict | None] = mapped_column(JSONB)
    canonical_result_storage_key: Mapped[str | None] = mapped_column(Text)
    compression_method: Mapped[str | None] = mapped_column(String(16))
    compressed_sha256_checksum: Mapped[str | None] = mapped_column(String(64))
    content_sha256_checksum: Mapped[str | None] = mapped_column(String(64))
    compressed_size: Mapped[int | None] = mapped_column(Integer)
    uncompressed_size: Mapped[int | None] = mapped_column(Integer)
    reported_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_stage: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NormalizedLineItemProjection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Project one normalized line item for safe tenant-scoped reads."""
    __tablename__ = "normalized_line_items"
    __table_args__ = (UniqueConstraint("normalization_run_id", "source_candidate_id", name="uq_normalized_line_item_candidate"),)
    normalization_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("normalization_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    source_candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sku_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("skus.id", ondelete="RESTRICT"))
    sku_code: Mapped[str | None] = mapped_column(String(255))
    sku_name: Mapped[str | None] = mapped_column(String(512))
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)


class NormalizationIssueProjection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Project finalization and review issues without source content."""
    __tablename__ = "normalization_issues"
    __table_args__ = (UniqueConstraint("normalization_run_id", "source_order", name="uq_normalization_issue_order"),)
    normalization_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("normalization_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    classification: Mapped[str] = mapped_column(String(64), nullable=False)
    location: Mapped[str] = mapped_column(String(200), nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(String(128))
    field_name: Mapped[str | None] = mapped_column(String(100))
    hard: Mapped[bool] = mapped_column(nullable=False, default=False)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)


class NormalizationSemanticFindingProjection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Project bounded semantic findings and correction routes."""
    __tablename__ = "normalization_semantic_findings"
    __table_args__ = (UniqueConstraint("normalization_run_id", "review_item_id", name="uq_normalization_semantic_item"),)
    normalization_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("normalization_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    review_item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[str | None] = mapped_column(String(64))
    candidate_id: Mapped[str | None] = mapped_column(String(128))
    field_name: Mapped[str | None] = mapped_column(String(100))
    evidence_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    rationale: Mapped[str | None] = mapped_column(Text)
