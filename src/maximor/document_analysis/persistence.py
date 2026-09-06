"""Persist and reload validated document-analysis results without invoking Claude.

This service receives one validated semantic result plus bounded runtime metrics,
stores a reproducible compressed artifact and projections, and returns validated
results. It does not run agents, read PDFs, or change document/job statuses.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maximor.db.models import (
    Document,
    DocumentAnalysisEvidenceReference,
    DocumentAnalysisRun,
    DocumentBlock,
    DocumentCommercialStatusAssessment,
    DocumentGlobalTerm,
    DocumentGlobalTermCandidate,
    DocumentPage,
    DocumentProcessingRun,
    DocumentProductCandidate,
    DocumentTable,
    Organization,
)
from maximor.document_analysis.agent import DocumentAnalysisRuntimeSummary
from maximor.document_analysis.contracts import DocumentAnalysisRequest
from maximor.document_analysis.errors import DocumentAnalysisPersistenceError
from maximor.document_analysis.schemas import DocumentAnalysisResult, EvidenceReference
from maximor.document_analysis.validation import validate_document_analysis_result
from maximor.document_analysis.versions import ORDER_FORM_ANALYSIS_SKILL_VERSION
from maximor.preprocessing.persistence import (
    MAXIMUM_COMPRESSED_RESULT_BYTES,
    MAXIMUM_UNCOMPRESSED_RESULT_BYTES,
    RESULT_COMPRESSION,
    canonical_json_bytes,
    compress_canonical_json,
    decompress_canonical_json,
)
from maximor.storage import ObjectStorage, StorageError


BOUNDING_BOX_TOLERANCE = 0.01


class DocumentAnalysisPersistenceService:
    """Own document-analysis run state, canonical artifacts, and SQL projections."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], storage: ObjectStorage) -> None:
        """Receive application database and object-storage dependencies explicitly."""
        self._sessions = sessions
        self._storage = storage

    async def create_run(
        self,
        *,
        run_id: uuid.UUID,
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
        preprocessing_run_id: uuid.UUID,
        attempt_number: int,
        schema_version: str,
        preprocessing_schema_version: str,
        prompt_version: str,
        skill_version: str,
        agent_version: str,
        model: str,
        started_at: datetime,
        processing_job_id: uuid.UUID | None = None,
    ) -> DocumentAnalysisRun:
        """Create one validated running attempt without calling Claude or fabricating output."""
        async with self._sessions() as session:
            async with session.begin():
                await self._validate_source(
                    session, organization_id, document_id, preprocessing_run_id,
                    preprocessing_schema_version,
                )
                existing = await session.get(DocumentAnalysisRun, run_id)
                if existing is not None:
                    if (
                        existing.organization_id == organization_id
                        and existing.document_id == document_id
                        and existing.preprocessing_run_id == preprocessing_run_id
                        and existing.attempt_number == attempt_number
                    ):
                        return existing
                    raise DocumentAnalysisPersistenceError("analysis_run_conflict")
                duplicate = await session.scalar(
                    select(DocumentAnalysisRun).where(
                        DocumentAnalysisRun.preprocessing_run_id == preprocessing_run_id,
                        DocumentAnalysisRun.attempt_number == attempt_number,
                    )
                )
                if duplicate is not None:
                    raise DocumentAnalysisPersistenceError("analysis_attempt_exists")
                run = DocumentAnalysisRun(
                    id=run_id,
                    organization_id=organization_id,
                    document_id=document_id,
                    preprocessing_run_id=preprocessing_run_id,
                    processing_job_id=processing_job_id,
                    attempt_number=attempt_number,
                    status="running",
                    schema_version=schema_version,
                    preprocessing_schema_version=preprocessing_schema_version,
                    prompt_version=prompt_version,
                    skill_version=skill_version,
                    agent_version=agent_version,
                    model=model,
                    started_at=started_at,
                    validation_status="pending",
                )
                session.add(run)
                return run

    async def save_completed_result(
        self,
        *,
        analysis_run_id: uuid.UUID,
        result: DocumentAnalysisResult,
        runtime: DocumentAnalysisRuntimeSummary | None = None,
    ) -> tuple[str, str]:
        """Save a validated result atomically, cleaning a new orphan artifact on failure."""
        if runtime is None:
            raise DocumentAnalysisPersistenceError("analysis_completion_runtime_missing")
        if validate_document_analysis_result(
            DocumentAnalysisRequest(
                organization_id=result.organization_id, document_id=result.document_id,
                preprocessing_run_id=result.preprocessing_run_id,
                preprocessing_schema_version=result.preprocessing_schema_version,
                document_analysis_schema_version=result.schema_version,
                prompt_version=result.prompt_version,
                skill_version=ORDER_FORM_ANALYSIS_SKILL_VERSION,
                agent_version=result.agent_version,
            ),
            result,
            runtime,
        ):
            raise DocumentAnalysisPersistenceError("analysis_completion_gate_failed")
        canonical = canonical_json_bytes(result)
        if len(canonical) > MAXIMUM_UNCOMPRESSED_RESULT_BYTES:
            raise DocumentAnalysisPersistenceError("analysis_artifact_too_large")
        content_checksum = hashlib.sha256(canonical).hexdigest()

        async with self._sessions() as session:
            run = await session.get(DocumentAnalysisRun, analysis_run_id)
            if run is None:
                raise DocumentAnalysisPersistenceError("analysis_run_not_found")
            if run.status == "completed":
                if run.content_sha256_checksum == content_checksum and run.canonical_result_storage_key and run.compressed_sha256_checksum:
                    return run.canonical_result_storage_key, run.compressed_sha256_checksum
                raise DocumentAnalysisPersistenceError("analysis_result_conflict")
            if run.status != "running":
                raise DocumentAnalysisPersistenceError("analysis_run_not_running")

        compressed = compress_canonical_json(canonical)
        if len(compressed) > MAXIMUM_COMPRESSED_RESULT_BYTES:
            raise DocumentAnalysisPersistenceError("analysis_artifact_too_large")
        key = self._artifact_key(result, analysis_run_id)

        async def chunks():
            yield compressed

        try:
            metadata = await self._storage.put(
                key, chunks(), maximum_bytes=MAXIMUM_COMPRESSED_RESULT_BYTES
            )
        except StorageError:
            raise DocumentAnalysisPersistenceError("analysis_artifact_storage_failed") from None

        try:
            async with self._sessions() as session:
                async with session.begin():
                    run = await session.scalar(
                        select(DocumentAnalysisRun)
                        .where(DocumentAnalysisRun.id == analysis_run_id)
                        .with_for_update()
                    )
                    if run is None:
                        raise DocumentAnalysisPersistenceError("analysis_run_not_found")
                    if run.status == "completed":
                        if run.content_sha256_checksum == content_checksum and run.canonical_result_storage_key and run.compressed_sha256_checksum:
                            await self._storage.delete(key)
                            return run.canonical_result_storage_key, run.compressed_sha256_checksum
                        raise DocumentAnalysisPersistenceError("analysis_result_conflict")
                    if run.status != "running":
                        raise DocumentAnalysisPersistenceError("analysis_run_not_running")
                    evidence = await self._validate_result(session, run, result)
                    await self._add_projections(session, run, result, evidence)
                    run.status = "completed"
                    run.validation_status = "validated"
                    run.canonical_result_storage_key = key
                    run.compression_method = RESULT_COMPRESSION
                    run.compressed_sha256_checksum = metadata.sha256_checksum
                    run.content_sha256_checksum = content_checksum
                    run.compressed_size = metadata.size
                    run.uncompressed_size = len(canonical)
                    run.completed_at = runtime.completed_at if runtime and runtime.completed_at else datetime.now(UTC)
                    self._apply_runtime(run, runtime)
            return key, metadata.sha256_checksum
        except Exception:
            await self._storage.delete(key)
            raise

    async def mark_run_failed(
        self,
        analysis_run_id: uuid.UUID,
        *,
        error_code: str,
        error_message: str,
        terminal_reason: str | None = None,
        completed_at: datetime | None = None,
        runtime: DocumentAnalysisRuntimeSummary | None = None,
    ) -> None:
        """Record only a bounded safe terminal failure and never overwrite completion."""
        async with self._sessions() as session:
            async with session.begin():
                run = await session.get(DocumentAnalysisRun, analysis_run_id)
                if run is None:
                    raise DocumentAnalysisPersistenceError("analysis_run_not_found")
                if run.status == "completed":
                    raise DocumentAnalysisPersistenceError("analysis_run_already_completed")
                run.status = "failed"
                run.validation_status = "failed"
                run.error_code = error_code[:100]
                run.error_message = error_message[:1000]
                run.terminal_reason = terminal_reason[:100] if terminal_reason else None
                run.completed_at = completed_at or datetime.now(UTC)
                self._apply_runtime(run, runtime)
                run.canonical_result_storage_key = None
                run.compression_method = None
                run.compressed_sha256_checksum = None
                run.content_sha256_checksum = None
                run.compressed_size = None
                run.uncompressed_size = None

    async def load_completed_result(
        self, *, organization_id: uuid.UUID, analysis_run_id: uuid.UUID
    ) -> DocumentAnalysisResult:
        """Load, verify, decompress, and validate a completed artifact without Claude or PDFs."""
        async with self._sessions() as session:
            run = await session.scalar(
                select(DocumentAnalysisRun).where(
                    DocumentAnalysisRun.id == analysis_run_id,
                    DocumentAnalysisRun.organization_id == organization_id,
                    DocumentAnalysisRun.status == "completed",
                )
            )
        if (
            run is None
            or not run.canonical_result_storage_key
            or not run.compressed_sha256_checksum
            or not run.content_sha256_checksum
            or run.compression_method != RESULT_COMPRESSION
        ):
            raise DocumentAnalysisPersistenceError("completed_analysis_result_unavailable")
        try:
            stored = await self._storage.read(
                run.canonical_result_storage_key,
                maximum_bytes=MAXIMUM_COMPRESSED_RESULT_BYTES,
            )
        except StorageError:
            raise DocumentAnalysisPersistenceError("analysis_artifact_unavailable") from None
        if hashlib.sha256(stored).hexdigest() != run.compressed_sha256_checksum:
            raise DocumentAnalysisPersistenceError("analysis_artifact_checksum_mismatch")
        if run.compressed_size is not None and len(stored) != run.compressed_size:
            raise DocumentAnalysisPersistenceError("analysis_artifact_size_mismatch")
        try:
            canonical = decompress_canonical_json(
                stored, maximum_bytes=MAXIMUM_UNCOMPRESSED_RESULT_BYTES
            )
            result = DocumentAnalysisResult.model_validate_json(canonical)
        except (ValueError, UnicodeDecodeError):
            raise DocumentAnalysisPersistenceError("analysis_artifact_invalid") from None
        if (
            run.uncompressed_size is not None and len(canonical) != run.uncompressed_size
        ) or hashlib.sha256(canonical).hexdigest() != run.content_sha256_checksum:
            raise DocumentAnalysisPersistenceError("analysis_artifact_checksum_mismatch")
        if (
            result.organization_id != run.organization_id
            or result.document_id != run.document_id
            or result.preprocessing_run_id != run.preprocessing_run_id
            or result.schema_version != run.schema_version
            or result.preprocessing_schema_version != run.preprocessing_schema_version
            or result.prompt_version != run.prompt_version
            or result.agent_version != run.agent_version
        ):
            raise DocumentAnalysisPersistenceError("analysis_artifact_identity_mismatch")
        return result

    async def latest_run(
        self, *, organization_id: uuid.UUID, document_id: uuid.UUID
    ) -> DocumentAnalysisRun | None:
        """Return one tenant-scoped latest run without exposing artifact internals."""
        async with self._sessions() as session:
            return await session.scalar(
                select(DocumentAnalysisRun)
                .where(
                    DocumentAnalysisRun.organization_id == organization_id,
                    DocumentAnalysisRun.document_id == document_id,
                )
                .order_by(DocumentAnalysisRun.created_at.desc(), DocumentAnalysisRun.id.desc())
            )

    @staticmethod
    def _artifact_key(result: DocumentAnalysisResult, analysis_run_id: uuid.UUID) -> str:
        """Construct the internal relative canonical-artifact key for one attempt."""
        return (
            f"organizations/{result.organization_id}/documents/{result.document_id}/"
            f"analysis/{analysis_run_id}/document-analysis-result.json.gz"
        )

    async def _validate_source(
        self, session: AsyncSession, organization_id: uuid.UUID, document_id: uuid.UUID,
        preprocessing_run_id: uuid.UUID, preprocessing_schema_version: str,
    ) -> DocumentProcessingRun:
        """Require a tenant-owned completed preprocessing run for every analysis attempt."""
        if await session.get(Organization, organization_id) is None:
            raise DocumentAnalysisPersistenceError("analysis_organization_not_found")
        document = await session.scalar(select(Document).where(Document.id == document_id, Document.organization_id == organization_id))
        if document is None:
            raise DocumentAnalysisPersistenceError("analysis_document_not_found")
        preprocessing_run = await session.scalar(
            select(DocumentProcessingRun).where(
                DocumentProcessingRun.id == preprocessing_run_id,
                DocumentProcessingRun.organization_id == organization_id,
                DocumentProcessingRun.document_id == document_id,
            )
        )
        if preprocessing_run is None:
            raise DocumentAnalysisPersistenceError("analysis_preprocessing_run_not_found")
        if preprocessing_run.status != "completed":
            raise DocumentAnalysisPersistenceError("analysis_preprocessing_run_not_completed")
        if preprocessing_run.schema_version != preprocessing_schema_version:
            raise DocumentAnalysisPersistenceError("analysis_preprocessing_schema_mismatch")
        return preprocessing_run

    async def _validate_result(
        self, session: AsyncSession, run: DocumentAnalysisRun, result: DocumentAnalysisResult,
    ) -> list[tuple[str, str, int, EvidenceReference, DocumentBlock | DocumentTable]]:
        """Resolve each semantic evidence link to exactly one matching persisted projection."""
        if (
            result.organization_id != run.organization_id
            or result.document_id != run.document_id
            or result.preprocessing_run_id != run.preprocessing_run_id
            or result.schema_version != run.schema_version
            or result.preprocessing_schema_version != run.preprocessing_schema_version
            or result.prompt_version != run.prompt_version
            or result.agent_version != run.agent_version
        ):
            raise DocumentAnalysisPersistenceError("analysis_result_identity_mismatch")
        await self._validate_source(
            session, run.organization_id, run.document_id, run.preprocessing_run_id,
            run.preprocessing_schema_version,
        )
        references: list[tuple[str, str, int, EvidenceReference]] = [
            ("document_analysis_result", "result", order, reference)
            for order, reference in enumerate(result.evidence_references)
        ]
        for owner_type, items, attr in (
            ("contract_structure", result.contract_structure, "structure_id"),
            ("pricing_section", result.pricing_sections, "section_id"),
            ("global_term", result.global_terms, "term_id"),
            ("product_candidate", result.product_candidates, "candidate_id"),
            ("commercial_status_assessment", result.commercial_statuses, "assessment_id"),
        ):
            for item in items:
                references.extend((owner_type, getattr(item, attr), order, reference) for order, reference in enumerate(item.evidence))
        resolved = []
        for owner_type, owner_id, order, reference in references:
            resolved.append((owner_type, owner_id, order, reference, await self._resolve_evidence(session, run, reference)))
        return resolved

    async def _resolve_evidence(
        self, session: AsyncSession, run: DocumentAnalysisRun, reference: EvidenceReference,
    ) -> DocumentBlock | DocumentTable:
        """Validate one evidence record against its exact persisted page/source/geometry."""
        page = await session.scalar(select(DocumentPage).where(DocumentPage.processing_run_id == run.preprocessing_run_id, DocumentPage.organization_id == run.organization_id, DocumentPage.page_number == reference.page_number))
        if page is None:
            raise DocumentAnalysisPersistenceError("analysis_evidence_page_not_found")
        if reference.block_id is not None:
            block = await session.scalar(select(DocumentBlock).where(DocumentBlock.processing_run_id == run.preprocessing_run_id, DocumentBlock.organization_id == run.organization_id, DocumentBlock.document_page_id == page.id, DocumentBlock.external_block_id == reference.block_id))
            if block is None or block.representation != reference.representation.value:
                raise DocumentAnalysisPersistenceError("analysis_evidence_mismatch")
            if reference.extraction_source is not None and block.extraction_source != reference.extraction_source.value:
                raise DocumentAnalysisPersistenceError("analysis_evidence_mismatch")
            self._validate_bounding_box(reference, block)
            return block
        table = await session.scalar(select(DocumentTable).where(DocumentTable.processing_run_id == run.preprocessing_run_id, DocumentTable.organization_id == run.organization_id, DocumentTable.document_page_id == page.id, DocumentTable.external_table_id == reference.table_id))
        if table is None or reference.representation.value != "table" or reference.extraction_source is not None:
            raise DocumentAnalysisPersistenceError("analysis_evidence_mismatch")
        self._validate_bounding_box(reference, table)
        return table

    @staticmethod
    def _validate_bounding_box(reference: EvidenceReference, target: DocumentBlock | DocumentTable) -> None:
        """Compare optional evidence geometry with persisted coordinates at a fixed tolerance."""
        if reference.bounding_box is None:
            return
        expected = (target.x0, target.y0, target.x1, target.y1)
        actual = (reference.bounding_box.x0, reference.bounding_box.y0, reference.bounding_box.x1, reference.bounding_box.y1)
        if any(abs(left - right) > BOUNDING_BOX_TOLERANCE for left, right in zip(expected, actual, strict=True)):
            raise DocumentAnalysisPersistenceError("analysis_evidence_bounding_box_mismatch")

    @staticmethod
    async def _add_projections(
        session: AsyncSession, run: DocumentAnalysisRun, result: DocumentAnalysisResult,
        evidence: list[tuple[str, str, int, EvidenceReference, DocumentBlock | DocumentTable]],
    ) -> None:
        """Insert deterministic candidate, status, and evidence projections within one transaction."""
        now = datetime.now(UTC)
        candidate_rows: dict[str, DocumentProductCandidate] = {}
        for order, candidate in enumerate(result.product_candidates):
            row = DocumentProductCandidate(
                analysis_run_id=run.id, organization_id=run.organization_id,
                external_candidate_id=candidate.candidate_id, source_order=order,
                raw_name=candidate.raw_name,
                raw_attributes=candidate.raw_attributes,
                evidence_count=len(candidate.evidence), created_at=now,
            )
            session.add(row)
            candidate_rows[candidate.candidate_id] = row
        await session.flush()
        for order, assessment in enumerate(result.commercial_statuses):
            candidate = candidate_rows.get(assessment.candidate_id) if assessment.candidate_id else None
            if assessment.candidate_id is not None and candidate is None:
                raise DocumentAnalysisPersistenceError("analysis_candidate_relationship_mismatch")
            session.add(DocumentCommercialStatusAssessment(
                analysis_run_id=run.id, product_candidate_id=candidate.id if candidate else None,
                organization_id=run.organization_id, external_assessment_id=assessment.assessment_id,
                commercial_status=assessment.status.value, raw_rationale=assessment.raw_rationale,
                source_order=order, created_at=now,
            ))
        term_rows: dict[str, DocumentGlobalTerm] = {}
        for order, term in enumerate(result.global_terms):
            row = DocumentGlobalTerm(
                analysis_run_id=run.id, organization_id=run.organization_id,
                external_term_id=term.term_id, raw_name=term.raw_name,
                raw_value=term.raw_value, applicability_scope=term.applicability_scope.value,
                source_order=order, evidence_count=len(term.evidence), created_at=now,
            )
            session.add(row)
            term_rows[term.term_id] = row
        await session.flush()
        for term in result.global_terms:
            if term.applicability_scope.value != "candidate":
                continue
            term_row = term_rows[term.term_id]
            for candidate_id in term.applies_to_candidate_ids:
                candidate_row = candidate_rows.get(candidate_id)
                if candidate_row is None:
                    raise DocumentAnalysisPersistenceError("analysis_term_candidate_mismatch")
                session.add(DocumentGlobalTermCandidate(
                    global_term_id=term_row.id,
                    product_candidate_id=candidate_row.id,
                    analysis_run_id=run.id,
                    organization_id=run.organization_id,
                    candidate_external_id=candidate_id,
                    created_at=now,
                ))
        for owner_type, owner_id, order, reference, target in evidence:
            is_block = isinstance(target, DocumentBlock)
            session.add(DocumentAnalysisEvidenceReference(
                analysis_run_id=run.id, organization_id=run.organization_id,
                owner_type=owner_type, owner_external_id=owner_id,
                preprocessing_run_id=run.preprocessing_run_id, page_number=reference.page_number,
                document_block_id=target.id if is_block else None,
                document_table_id=None if is_block else target.id,
                external_block_id=reference.block_id if is_block else None,
                external_table_id=None if is_block else reference.table_id,
                representation=reference.representation.value,
                extraction_source=reference.extraction_source.value if reference.extraction_source else None,
                x0=reference.bounding_box.x0 if reference.bounding_box else None,
                y0=reference.bounding_box.y0 if reference.bounding_box else None,
                x1=reference.bounding_box.x1 if reference.bounding_box else None,
                y1=reference.bounding_box.y1 if reference.bounding_box else None,
                evidence_order=order, created_at=now,
            ))

    @staticmethod
    def _apply_runtime(run: DocumentAnalysisRun, runtime: DocumentAnalysisRuntimeSummary | None) -> None:
        """Copy only approved bounded operational metrics into the workflow record."""
        if runtime is None:
            return
        run.input_tokens = runtime.input_tokens
        run.cache_creation_input_tokens = runtime.cache_creation_input_tokens
        run.cache_read_input_tokens = runtime.cache_read_input_tokens
        run.output_tokens = runtime.output_tokens
        run.model_usage = runtime.model_usage or None
        run.tool_call_count = runtime.tool_call_count
        run.turn_count = runtime.turn_count
        run.reported_cost_usd = Decimal(str(runtime.cost_usd)) if runtime.cost_usd is not None else None
        run.terminal_reason = runtime.terminal_reason[:100] if runtime.terminal_reason else None
        run.runtime_diagnostics = runtime.persistence_diagnostics()
        run.correction_attempt_count = runtime.correction_attempt_count
