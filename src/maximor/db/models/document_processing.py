"""Relational projections for validated deterministic preprocessing results."""
import uuid
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from maximor.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

class DocumentProcessingRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persist one tenant-scoped preprocessing attempt and its canonical artifact."""
    __tablename__='document_processing_runs'
    __table_args__=(UniqueConstraint('processing_job_id','attempt_number',name='uq_runs_job_attempt'),CheckConstraint("status IN ('running','completed','failed')",name='status_allowed'),CheckConstraint("result_compression IS NULL OR result_compression = 'gzip'",name='result_compression_allowed'),ForeignKeyConstraint(['organization_id','document_id'],['documents.organization_id','documents.id'],name='fk_runs_tenant_document',ondelete='CASCADE'))
    organization_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('organizations.id',ondelete='CASCADE'),nullable=False,index=True)
    document_id: Mapped[uuid.UUID]=mapped_column(nullable=False,index=True)
    processing_job_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('processing_jobs.id',ondelete='CASCADE'),nullable=False,index=True)
    attempt_number: Mapped[int]=mapped_column(Integer,nullable=False)
    status: Mapped[str]=mapped_column(String(32),nullable=False)
    schema_version: Mapped[str]=mapped_column(String(50),nullable=False)
    processor_version: Mapped[str]=mapped_column(String(100),nullable=False)
    original_document_checksum: Mapped[str]=mapped_column(String(64),nullable=False)
    page_count: Mapped[int|None]=mapped_column(Integer)
    inspection: Mapped[dict|None]=mapped_column(JSONB)
    warnings: Mapped[list|None]=mapped_column(JSONB)
    result_storage_key: Mapped[str|None]=mapped_column(Text)
    result_sha256_checksum: Mapped[str|None]=mapped_column(String(64))
    result_content_sha256_checksum: Mapped[str|None]=mapped_column(String(64))
    result_compression: Mapped[str|None]=mapped_column(String(16))
    result_uncompressed_size: Mapped[int|None]=mapped_column(BigInteger)
    result_compressed_size: Mapped[int|None]=mapped_column(BigInteger)
    error_code: Mapped[str|None]=mapped_column(String(100))
    error_message: Mapped[str|None]=mapped_column(Text)
    started_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)
    completed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))

class DocumentPage(UUIDPrimaryKeyMixin, Base):
    """Persist searchable per-page facts and safe render references."""
    __tablename__='document_pages'
    __table_args__=(UniqueConstraint('processing_run_id','page_number',name='uq_pages_run_number'),)
    processing_run_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('document_processing_runs.id',ondelete='CASCADE'),nullable=False,index=True)
    organization_id: Mapped[uuid.UUID]=mapped_column(nullable=False,index=True)
    page_number: Mapped[int]=mapped_column(Integer,nullable=False)
    width_points: Mapped[float]=mapped_column(nullable=False); height_points: Mapped[float]=mapped_column(nullable=False); rotation_degrees: Mapped[int]=mapped_column(Integer,nullable=False)
    native_plain_text: Mapped[str]=mapped_column(Text,nullable=False); native_character_count: Mapped[int]=mapped_column(Integer,nullable=False); native_word_count: Mapped[int]=mapped_column(Integer,nullable=False)
    ocr_status: Mapped[str]=mapped_column(String(32),nullable=False); ocr_plain_text: Mapped[str|None]=mapped_column(Text)
    quality: Mapped[dict]=mapped_column(JSONB,nullable=False); render_storage_key: Mapped[str]=mapped_column(Text,nullable=False); render_media_type: Mapped[str]=mapped_column(String(32),nullable=False); render_pixel_width: Mapped[int]=mapped_column(Integer,nullable=False); render_pixel_height: Mapped[int]=mapped_column(Integer,nullable=False); render_dpi: Mapped[int]=mapped_column(Integer,nullable=False); render_checksum: Mapped[str]=mapped_column(String(64),nullable=False); warnings: Mapped[list|None]=mapped_column(JSONB)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)

class DocumentBlock(UUIDPrimaryKeyMixin, Base):
    """Persist evidence-addressable native, layout, and OCR blocks separately."""
    __tablename__='document_blocks'
    __table_args__=(UniqueConstraint('processing_run_id','external_block_id',name='uq_blocks_run_external_id'),)
    processing_run_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('document_processing_runs.id',ondelete='CASCADE'),nullable=False,index=True)
    document_page_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('document_pages.id',ondelete='CASCADE'),nullable=False,index=True)
    organization_id: Mapped[uuid.UUID]=mapped_column(nullable=False,index=True); external_block_id: Mapped[str]=mapped_column(String(128),nullable=False); representation: Mapped[str]=mapped_column(String(32),nullable=False); extraction_source: Mapped[str]=mapped_column(String(32),nullable=False); block_type: Mapped[str|None]=mapped_column(String(32)); reading_order: Mapped[int]=mapped_column(Integer,nullable=False); text: Mapped[str|None]=mapped_column(Text); x0: Mapped[float]=mapped_column(nullable=False); y0: Mapped[float]=mapped_column(nullable=False); x1: Mapped[float]=mapped_column(nullable=False); y1: Mapped[float]=mapped_column(nullable=False); font: Mapped[dict|None]=mapped_column(JSONB); ocr_confidence: Mapped[float|None]=mapped_column(); created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)

class DocumentTable(UUIDPrimaryKeyMixin, Base):
    """Persist a physical table projection while retaining rows as validated JSON."""
    __tablename__='document_tables'
    __table_args__=(UniqueConstraint('processing_run_id','external_table_id',name='uq_tables_run_external_id'),UniqueConstraint('document_page_id','table_index',name='uq_tables_page_index'))
    processing_run_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('document_processing_runs.id',ondelete='CASCADE'),nullable=False,index=True); document_page_id: Mapped[uuid.UUID]=mapped_column(ForeignKey('document_pages.id',ondelete='CASCADE'),nullable=False,index=True); organization_id: Mapped[uuid.UUID]=mapped_column(nullable=False,index=True); external_table_id: Mapped[str]=mapped_column(String(128),nullable=False); table_index: Mapped[int]=mapped_column(Integer,nullable=False); x0: Mapped[float]=mapped_column(nullable=False); y0: Mapped[float]=mapped_column(nullable=False); x1: Mapped[float]=mapped_column(nullable=False); y1: Mapped[float]=mapped_column(nullable=False); rows: Mapped[list]=mapped_column(JSONB,nullable=False); warnings: Mapped[list|None]=mapped_column(JSONB); created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)
