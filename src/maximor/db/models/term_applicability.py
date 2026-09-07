"""Persistence projections for combined term triage and applicability results."""
import uuid
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from maximor.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

class TermApplicabilityRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Store one immutable combined enrichment attempt and artifact metadata."""
    __tablename__ = "term_applicability_runs"
    __table_args__ = (UniqueConstraint("analysis_run_id", "attempt_number", name="uq_term_applicability_run_attempt"), CheckConstraint("status IN ('running','completed','failed')", name="term_applicability_status_allowed"), CheckConstraint("status <> 'completed' OR canonical_result_storage_key IS NOT NULL", name="term_applicability_completed_artifact_required"))
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_analysis_runs.id", ondelete="RESTRICT"), nullable=False, index=True)
    processing_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("processing_jobs.id", ondelete="SET NULL"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    skill_version: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    runtime_diagnostics: Mapped[dict | None] = mapped_column(JSONB)
    canonical_result_storage_key: Mapped[str | None] = mapped_column(Text)
    compressed_sha256_checksum: Mapped[str | None] = mapped_column(String(64))
    content_sha256_checksum: Mapped[str | None] = mapped_column(String(64))
    compressed_size: Mapped[int | None] = mapped_column(Integer)
    uncompressed_size: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)

class TermTriageDecisionProjection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Project one triage decision for search and auditing."""
    __tablename__ = "term_triage_decisions"
    __table_args__ = (UniqueConstraint("run_id", "term_id", name="uq_term_triage_decision_term"),)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("term_applicability_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    term_id: Mapped[str] = mapped_column(String(128), nullable=False)
    disposition: Mapped[str] = mapped_column(String(64), nullable=False)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)

class TermApplicabilityDecisionProjection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Project one selected-term applicability decision."""
    __tablename__ = "term_applicability_decisions"
    __table_args__ = (UniqueConstraint("run_id", "term_id", name="uq_term_applicability_decision_term"),)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("term_applicability_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    term_id: Mapped[str] = mapped_column(String(128), nullable=False)
    disposition: Mapped[str] = mapped_column(String(64), nullable=False)
    applicability_scope: Mapped[str | None] = mapped_column(String(32))
    candidate_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)

class CandidateCommercialFactCoverageProjection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Project expected/extracted/unresolved candidate fact coverage."""
    __tablename__ = "candidate_commercial_fact_coverages"
    __table_args__ = (UniqueConstraint("run_id", "candidate_id", name="uq_candidate_fact_coverage"),)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("term_applicability_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    # Explicit short name: the naming-convention-derived name for this
    # table+column+referent combination is 68 characters, over Postgres'
    # 63-character identifier limit.
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE", name="fk_candidate_fact_coverage_org"), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    expected_fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    extracted_fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    unresolved_fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

class CandidateCommercialFactProjection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Project one raw candidate commercial fact without normalization."""
    __tablename__ = "candidate_commercial_facts"
    __table_args__ = (UniqueConstraint("run_id", "fact_id", name="uq_candidate_commercial_fact"),)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("term_applicability_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fact_id: Mapped[str] = mapped_column(String(200), nullable=False)
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_value: Mapped[str] = mapped_column(Text, nullable=False)
    raw_period_label: Mapped[str | None] = mapped_column(String(100))
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

class TermApplicabilityEvidenceProjection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Project evidence references owned by enrichment output entities."""
    __tablename__ = "term_applicability_evidence_references"
    __table_args__ = (UniqueConstraint("run_id", "owner_type", "owner_id", "source_order", name="uq_term_applicability_evidence_order"),)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("term_applicability_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    # Explicit short name: the naming-convention-derived name for this
    # table+column+referent combination is 71 characters, over Postgres'
    # 63-character identifier limit.
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE", name="fk_term_evidence_refs_org"), nullable=False, index=True)
    owner_type: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(200), nullable=False)
    preprocessing_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_processing_runs.id", ondelete="RESTRICT"), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    external_block_id: Mapped[str | None] = mapped_column(String(128))
    external_table_id: Mapped[str | None] = mapped_column(String(128))
    representation: Mapped[str] = mapped_column(String(32), nullable=False)
    extraction_source: Mapped[str | None] = mapped_column(String(32))
    bounding_box: Mapped[dict | None] = mapped_column(JSONB)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)
