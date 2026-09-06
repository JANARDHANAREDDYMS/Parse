"""Relational projections and workflow state for validated SKU-mapping decisions.

These models receive a validated `SkuMappingDecision` and record searchable
facts plus a canonical-artifact reference, mirroring `document_analysis_runs`
and its projections. They do not call Claude, load PDFs, or store prompts.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maximor.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SkuMappingRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persist one tenant-scoped SKU-mapping attempt and its accepted canonical result.

    Scoped to `document_product_candidate_id` (the internal UUID row in
    `document_product_candidates`), not the external candidate identifier
    `SkuMappingTask` uses — the external string stays inside the canonical
    artifact/projection as reference data only. `supersedes_mapping_run_id`
    is a forward lineage link: a catalog change produces a new row pointing
    back at the run it replaces, and the old row's `status` is never mutated
    — "superseded" is derived from whether any row points back at it.
    """

    __tablename__ = "sku_mapping_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["documents.organization_id", "documents.id"],
            name="fk_sku_mapping_runs_tenant_document",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "document_product_candidate_id", "attempt_number",
            name="uq_sku_mapping_runs_candidate_attempt",
        ),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="status_allowed"),
        CheckConstraint(
            "status <> 'completed' OR (canonical_result_storage_key IS NOT NULL "
            "AND completed_at IS NOT NULL AND validation_status = 'validated' "
            "AND catalog_version_id IS NOT NULL)",
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
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_analysis_runs.id", ondelete="RESTRICT"), nullable=False, index=True)
    document_product_candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_product_candidates.id", ondelete="RESTRICT"), nullable=False, index=True)
    processing_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("processing_jobs.id", ondelete="SET NULL"), index=True)
    # Nullable: unlike preprocessing_run_id in document_analysis_runs (known
    # before the run starts), catalog_version_id is only known once this
    # run's own retrieve_skus call actually succeeds during execution — it
    # is filled in at completion time, and completed_result_required below
    # requires it to be set whenever status='completed'.
    catalog_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("catalog_versions.id", ondelete="RESTRICT"), index=True)
    supersedes_mapping_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sku_mapping_runs.id", ondelete="RESTRICT"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    document_analysis_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    skill_version: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(100), nullable=False)
    retriever_version: Mapped[str] = mapped_column(String(50), nullable=False)
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


class SkuMappingDecisionProjection(UUIDPrimaryKeyMixin, Base):
    """Project one accepted SKU-mapping decision without any semantic recomputation."""

    __tablename__ = "sku_mapping_decisions"
    __table_args__ = (
        UniqueConstraint("sku_mapping_run_id", name="uq_sku_mapping_decisions_run"),
        CheckConstraint("outcome IN ('match', 'no_match', 'ambiguous')", name="outcome_allowed"),
        CheckConstraint(
            "(outcome = 'match' AND sku_id IS NOT NULL AND sku_code IS NOT NULL AND sku_name IS NOT NULL) "
            "OR (outcome <> 'match' AND sku_id IS NULL AND sku_code IS NULL AND sku_name IS NULL)",
            name="match_fields_consistent",
        ),
    )

    sku_mapping_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sku_mapping_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    document_product_candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_product_candidates.id", ondelete="RESTRICT"), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    sku_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("skus.id", ondelete="RESTRICT"), index=True)
    sku_code: Mapped[str | None] = mapped_column(String(255))
    sku_name: Mapped[str | None] = mapped_column(String(512))
    considered_sku_ids: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False,
    )
    rationale: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkuMappingEvidenceReference(UUIDPrimaryKeyMixin, Base):
    """Persist one evidence link from an accepted SKU-mapping decision to preprocessing evidence.

    Named without "decision" in the table itself (unlike the Python class)
    purely to stay under PostgreSQL's 63-character identifier limit once
    combined with a column name in an auto-generated index name.
    """

    __tablename__ = "sku_mapping_evidence_references"
    __table_args__ = (
        UniqueConstraint(
            "sku_mapping_run_id", "evidence_order", name="uq_sku_mapping_evidence_run_order",
        ),
        CheckConstraint(
            "(document_block_id IS NOT NULL AND document_table_id IS NULL) OR "
            "(document_block_id IS NULL AND document_table_id IS NOT NULL)",
            name="one_preprocessing_target",
        ),
    )

    sku_mapping_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sku_mapping_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
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
