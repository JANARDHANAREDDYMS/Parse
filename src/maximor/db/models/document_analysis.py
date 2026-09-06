"""Relational projections and workflow state for validated document analysis results.

These models receive validated semantic output and record searchable facts plus a
canonical-artifact reference. They do not call Claude, load PDFs, or store prompts.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maximor.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DocumentAnalysisRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persist one tenant-scoped analysis attempt and its accepted canonical result."""

    __tablename__ = "document_analysis_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["documents.organization_id", "documents.id"],
            name="fk_document_analysis_runs_tenant_document",
            ondelete="CASCADE",
        ),
        UniqueConstraint("preprocessing_run_id", "attempt_number", name="uq_analysis_runs_preprocessing_attempt"),
        # Supports a tenant-safe composite FK from processing_jobs (organization_id,
        # document_id, analysis_run_id) -> here, so a sku_mapping job's analysis_run_id
        # is provably scoped to the job's own organization and document.
        UniqueConstraint("organization_id", "document_id", "id", name="uq_document_analysis_runs_tenant_document_id"),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="status_allowed"),
        CheckConstraint(
            "status <> 'completed' OR (canonical_result_storage_key IS NOT NULL "
            "AND completed_at IS NOT NULL AND validation_status = 'validated')",
            name="completed_result_required",
        ),
        CheckConstraint(
            "status <> 'failed' OR canonical_result_storage_key IS NULL",
            name="failed_result_absent",
        ),
        CheckConstraint(
            "compression_method IS NULL OR compression_method = 'gzip'",
            name="compression_allowed",
        ),
        CheckConstraint(
            "correction_attempt_count >= 0",
            name="correction_attempt_count_nonnegative",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    preprocessing_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_processing_runs.id", ondelete="RESTRICT"), nullable=False, index=True)
    processing_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("processing_jobs.id", ondelete="SET NULL"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    preprocessing_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    skill_version: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_creation_input_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    model_usage: Mapped[dict | None] = mapped_column(JSONB)
    tool_call_count: Mapped[int | None] = mapped_column(Integer)
    turn_count: Mapped[int | None] = mapped_column(Integer)
    reported_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    terminal_reason: Mapped[str | None] = mapped_column(String(100))
    runtime_diagnostics: Mapped[dict | None] = mapped_column(JSONB)
    correction_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    canonical_result_storage_key: Mapped[str | None] = mapped_column(Text)
    compression_method: Mapped[str | None] = mapped_column(String(16))
    compressed_sha256_checksum: Mapped[str | None] = mapped_column(String(64))
    content_sha256_checksum: Mapped[str | None] = mapped_column(String(64))
    compressed_size: Mapped[int | None] = mapped_column(BigInteger)
    uncompressed_size: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)


class DocumentProductCandidate(UUIDPrimaryKeyMixin, Base):
    """Project one raw document product candidate without any SKU mapping decision."""

    __tablename__ = "document_product_candidates"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "external_candidate_id", name="uq_analysis_candidates_external_id"),
        # Supports a tenant-safe composite FK from processing_jobs (organization_id,
        # analysis_run_id, document_product_candidate_id) -> here, so a sku_mapping
        # job's candidate is provably scoped to the job's own organization and
        # exactly the analysis run it claims.
        UniqueConstraint("organization_id", "analysis_run_id", "id", name="uq_document_product_candidates_tenant_run_id"),
    )

    analysis_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    external_candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_name: Mapped[str] = mapped_column(Text, nullable=False)
    raw_attributes: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentCommercialStatusAssessment(UUIDPrimaryKeyMixin, Base):
    """Project one candidate commercial-status assessment from a completed analysis."""

    __tablename__ = "document_commercial_status_assessments"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "external_assessment_id", name="uq_analysis_assessments_external_id"),
    )

    analysis_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    product_candidate_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("document_product_candidates.id", ondelete="RESTRICT"), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    external_assessment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    commercial_status: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_rationale: Mapped[str | None] = mapped_column(Text)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentAnalysisEvidenceReference(UUIDPrimaryKeyMixin, Base):
    """Persist one evidence link from a semantic output owner to preprocessing evidence."""

    __tablename__ = "document_analysis_evidence_references"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "owner_type", "owner_external_id", "evidence_order", name="uq_analysis_evidence_owner_order"),
        CheckConstraint(
            "owner_type IN ('document_analysis_result', 'contract_structure', 'pricing_section', "
            "'global_term', 'product_candidate', 'commercial_status_assessment')",
            name="owner_type_allowed",
        ),
        CheckConstraint(
            "(document_block_id IS NOT NULL AND document_table_id IS NULL) OR "
            "(document_block_id IS NULL AND document_table_id IS NOT NULL)",
            name="one_preprocessing_target",
        ),
    )

    analysis_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_type: Mapped[str] = mapped_column(String(48), nullable=False)
    owner_external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    preprocessing_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_processing_runs.id", ondelete="RESTRICT"), nullable=False, index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    document_block_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("document_blocks.id", ondelete="RESTRICT"), index=True)
    document_table_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("document_tables.id", ondelete="RESTRICT"), index=True)
    external_block_id: Mapped[str | None] = mapped_column(String(128))
    external_table_id: Mapped[str | None] = mapped_column(String(128))
    representation: Mapped[str] = mapped_column(String(32), nullable=False)
    extraction_source: Mapped[str | None] = mapped_column(String(32))
    x0: Mapped[float | None] = mapped_column()
    y0: Mapped[float | None] = mapped_column()
    x1: Mapped[float | None] = mapped_column()
    y1: Mapped[float | None] = mapped_column()
    evidence_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentGlobalTerm(UUIDPrimaryKeyMixin, Base):
    """Project one raw contract term and its explicit applicability scope."""

    __tablename__ = "document_global_terms"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "external_term_id", name="uq_analysis_global_terms_external_id"),
    )

    analysis_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    external_term_id: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_name: Mapped[str] = mapped_column(Text, nullable=False)
    raw_value: Mapped[str | None] = mapped_column(Text)
    applicability_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentGlobalTermCandidate(UUIDPrimaryKeyMixin, Base):
    """Associate a candidate-scoped term with an internal candidate projection."""

    __tablename__ = "document_global_term_candidates"
    __table_args__ = (
        UniqueConstraint("global_term_id", "product_candidate_id", name="uq_global_term_candidate_link"),
        ForeignKeyConstraint(
            ["organization_id", "analysis_run_id", "product_candidate_id"],
            ["document_product_candidates.organization_id", "document_product_candidates.analysis_run_id", "document_product_candidates.id"],
            name="fk_global_term_candidates_tenant_candidate", ondelete="CASCADE",
        ),
    )

    global_term_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_global_terms.id", ondelete="CASCADE"), nullable=False, index=True)
    product_candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_product_candidates.id", ondelete="CASCADE"), nullable=False, index=True)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    candidate_external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
