"""Persist and reload validated Stage 5C normalization results.

The service stores one reproducible gzip JSON artifact and small relational
projections. It never reopens a PDF, calls Claude, or changes processing-job
status.
"""

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from maximor.db.models import NormalizationIssueProjection, NormalizationRun, NormalizationSemanticFindingProjection, NormalizedLineItemProjection
from maximor.normalization.schemas import NormalizationResult, NormalizationRunStatus
from maximor.normalization.versions import NORMALIZATION_RESULT_SCHEMA_VERSION
from maximor.preprocessing.persistence import MAXIMUM_COMPRESSED_RESULT_BYTES, MAXIMUM_UNCOMPRESSED_RESULT_BYTES, RESULT_COMPRESSION, canonical_json_bytes, compress_canonical_json, decompress_canonical_json
from maximor.storage import ObjectStorage, StorageError


class NormalizationPersistenceError(Exception):
    """Safe persistence failure with a stable code."""

    def __init__(self, code: str, message: str = "Normalization persistence failed."):
        super().__init__(message)
        self.code = code
        self.safe_message = message


class NormalizationPersistenceService:
    """Own normalization domain-run status, artifact integrity, and projections."""

    def __init__(self, sessions, storage: ObjectStorage):
        self._sessions = sessions
        self._storage = storage

    async def create_run(self, *, run_id: uuid.UUID, organization_id: uuid.UUID, document_id: uuid.UUID, preprocessing_run_id: uuid.UUID, analysis_run_id: uuid.UUID, term_applicability_run_id: uuid.UUID, attempt_number: int, processing_job_id: uuid.UUID | None, schema_version: str, finalization_policy_version: str, started_at: datetime) -> NormalizationRun:
        """Create one running attempt idempotently."""
        async with self._sessions() as session:
            async with session.begin():
                existing = await session.get(NormalizationRun, run_id)
                if existing is not None:
                    if existing.organization_id != organization_id or existing.document_id != document_id:
                        raise NormalizationPersistenceError("normalization_run_conflict")
                    return existing
                run = NormalizationRun(id=run_id, organization_id=organization_id, document_id=document_id, preprocessing_run_id=preprocessing_run_id, analysis_run_id=analysis_run_id, term_applicability_run_id=term_applicability_run_id, processing_job_id=processing_job_id, attempt_number=attempt_number, status="running", schema_version=schema_version, finalization_policy_version=finalization_policy_version, started_at=started_at)
                session.add(run)
                return run

    async def save_completed_result(self, *, run_id: uuid.UUID, result: NormalizationResult, runtime=None) -> tuple[str, str]:
        """Write canonical artifact and projections transactionally and idempotently."""
        canonical = canonical_json_bytes(result)
        if len(canonical) > MAXIMUM_UNCOMPRESSED_RESULT_BYTES:
            raise NormalizationPersistenceError("normalization_artifact_too_large")
        content_checksum = hashlib.sha256(canonical).hexdigest()
        compressed = compress_canonical_json(canonical)
        if len(compressed) > MAXIMUM_COMPRESSED_RESULT_BYTES:
            raise NormalizationPersistenceError("normalization_artifact_too_large")
        key = f"organizations/{result.organization_id}/documents/{result.document_id}/normalization/{run_id}/normalized-result.json.gz"
        # Check terminal/idempotent state before writing the deterministic key.
        # This prevents a conflicting retry from overwriting (and then deleting)
        # an already accepted artifact.
        async with self._sessions() as session:
            existing = await session.scalar(select(NormalizationRun).where(NormalizationRun.id == run_id))
        if existing is None:
            raise NormalizationPersistenceError("normalization_run_not_found")
        if existing.status in {"completed", "review_required", "failed_validation"}:
            if existing.content_sha256_checksum == content_checksum and existing.canonical_result_storage_key:
                return existing.canonical_result_storage_key, existing.compressed_sha256_checksum or ""
            raise NormalizationPersistenceError("normalization_result_conflict")
        if existing.status != "running":
            raise NormalizationPersistenceError("normalization_run_not_running")
        async def chunks():
            yield compressed
        try:
            metadata = await self._storage.put(key, chunks(), maximum_bytes=MAXIMUM_COMPRESSED_RESULT_BYTES)
        except StorageError:
            raise NormalizationPersistenceError("normalization_artifact_storage_failed") from None
        try:
            async with self._sessions() as session:
                async with session.begin():
                    run = await session.scalar(select(NormalizationRun).where(NormalizationRun.id == run_id).with_for_update())
                    if run is None:
                        raise NormalizationPersistenceError("normalization_run_not_found")
                    if run.status in {"completed", "review_required", "failed_validation"}:
                        if run.content_sha256_checksum == content_checksum and run.canonical_result_storage_key:
                            return run.canonical_result_storage_key, run.compressed_sha256_checksum or metadata.sha256_checksum
                        raise NormalizationPersistenceError("normalization_result_conflict")
                    if run.status != "running":
                        raise NormalizationPersistenceError("normalization_run_not_running")
                    run.status = result.status.value
                    run.canonical_result_storage_key = key
                    run.compression_method = RESULT_COMPRESSION
                    run.compressed_sha256_checksum = metadata.sha256_checksum
                    run.content_sha256_checksum = content_checksum
                    run.compressed_size = metadata.size
                    run.uncompressed_size = len(canonical)
                    run.completed_at = datetime.now(UTC)
                    run.runtime_diagnostics = runtime.safe_summary() if runtime is not None and hasattr(runtime, "safe_summary") else None
                    for index, item in enumerate(result.extraction.line_items):
                        session.add(NormalizedLineItemProjection(normalization_run_id=run.id, organization_id=run.organization_id, source_candidate_id=item.source_candidate_id, sku_id=item.sku_id, sku_code=item.sku_code, sku_name=item.sku_name, fields=item.model_dump(mode="json"), provenance={key: value.model_dump(mode="json") for key, value in item.field_provenance.items()}, source_order=index))
                    for index, issue in enumerate(result.finalization_issues):
                        session.add(NormalizationIssueProjection(normalization_run_id=run.id, organization_id=run.organization_id, code=issue.code, classification=issue.classification.value, location=issue.location, candidate_id=issue.candidate_id, field_name=issue.field_name, hard=issue.hard, source_order=index))
                    for finding in result.semantic_findings:
                        session.add(NormalizationSemanticFindingProjection(normalization_run_id=run.id, organization_id=run.organization_id, review_item_id=finding.review_item_id, outcome=finding.outcome, owner=finding.owner, candidate_id=finding.candidate_id, field_name=finding.field_name, evidence_ids=list(finding.evidence_ids), rationale=finding.rationale))
            return key, metadata.sha256_checksum
        except Exception:
            await self._storage.delete(key)
            raise

    async def load_completed_result(self, *, organization_id: uuid.UUID, run_id: uuid.UUID) -> NormalizationResult:
        """Reload and validate the canonical result without touching the source PDF."""
        async with self._sessions() as session:
            run = await session.scalar(select(NormalizationRun).where(NormalizationRun.id == run_id, NormalizationRun.organization_id == organization_id, NormalizationRun.status.in_(["completed", "review_required", "failed_validation"])))
        if run is None or not run.canonical_result_storage_key or not run.compressed_sha256_checksum or not run.content_sha256_checksum:
            raise NormalizationPersistenceError("normalization_result_unavailable")
        try:
            stored = await self._storage.read(run.canonical_result_storage_key, maximum_bytes=MAXIMUM_COMPRESSED_RESULT_BYTES)
        except StorageError:
            raise NormalizationPersistenceError("normalization_artifact_unavailable") from None
        if hashlib.sha256(stored).hexdigest() != run.compressed_sha256_checksum:
            raise NormalizationPersistenceError("normalization_artifact_checksum_mismatch")
        try:
            content = decompress_canonical_json(stored, maximum_bytes=MAXIMUM_UNCOMPRESSED_RESULT_BYTES)
            if hashlib.sha256(content).hexdigest() != run.content_sha256_checksum:
                raise NormalizationPersistenceError("normalization_content_checksum_mismatch")
            result = NormalizationResult.model_validate_json(content)
        except NormalizationPersistenceError:
            raise
        except Exception:
            raise NormalizationPersistenceError("normalization_artifact_invalid") from None
        if result.organization_id != organization_id or result.analysis_run_id != run.analysis_run_id:
            raise NormalizationPersistenceError("normalization_lineage_mismatch")
        return result

    async def mark_run_failed(self, run_id: uuid.UUID, *, error_code: str, error_stage: str | None = None, error_message: str = "Normalization failed.", runtime=None) -> None:
        """Record safe failed-domain metadata without overwriting accepted output."""
        async with self._sessions() as session:
            async with session.begin():
                run = await session.get(NormalizationRun, run_id)
                if run is None:
                    raise NormalizationPersistenceError("normalization_run_not_found")
                if run.status in {"completed", "review_required", "failed_validation"}:
                    raise NormalizationPersistenceError("normalization_run_already_terminal")
                run.status = "failed"
                run.error_code = error_code[:100]
                run.error_stage = error_stage[:100] if error_stage else None
                run.error_message = error_message[:1000]
                run.runtime_diagnostics = runtime.safe_summary() if runtime is not None and hasattr(runtime, "safe_summary") else None
                run.completed_at = datetime.now(UTC)

    async def latest_run(self, *, organization_id: uuid.UUID, document_id: uuid.UUID):
        """Return the newest tenant-scoped normalization domain run."""
        async with self._sessions() as session:
            return await session.scalar(select(NormalizationRun).where(NormalizationRun.organization_id == organization_id, NormalizationRun.document_id == document_id).order_by(NormalizationRun.attempt_number.desc()))
