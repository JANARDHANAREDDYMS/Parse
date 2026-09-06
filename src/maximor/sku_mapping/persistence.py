"""Persist and reload validated SKU-mapping run artifacts without invoking Claude.

This service receives one validated `SkuMappingRunArtifact` plus bounded
runtime metrics, stores a reproducible compressed artifact and projections,
and returns validated results. It does not run agents, read PDFs, run the
retriever, or change document/analysis-job statuses.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maximor.db.models import (
    Document,
    DocumentAnalysisRun,
    DocumentBlock,
    DocumentPage,
    DocumentProductCandidate,
    DocumentTable,
    Organization,
    Sku,
    SkuMappingDecisionProjection,
    SkuMappingEvidenceReference,
    SkuMappingRun,
)
from maximor.preprocessing.persistence import (
    MAXIMUM_COMPRESSED_RESULT_BYTES,
    MAXIMUM_UNCOMPRESSED_RESULT_BYTES,
    RESULT_COMPRESSION,
    canonical_json_bytes,
    compress_canonical_json,
    decompress_canonical_json,
)
from maximor.sku_mapping.agent import SkuMappingRuntimeSummary
from maximor.sku_mapping.contracts import SkuMappingRunArtifact
from maximor.sku_mapping.errors import SkuMappingPersistenceError
from maximor.sku_mapping.schemas import EvidenceReference, SkuMappingDecision, SkuMappingOutcome
from maximor.sku_mapping.validation import validate_sku_mapping_decision
from maximor.storage import ObjectStorage, StorageError

BOUNDING_BOX_TOLERANCE = 0.01


class SkuMappingPersistenceService:
    """Own SKU-mapping run state, canonical artifacts, and SQL projections."""

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
        analysis_run_id: uuid.UUID,
        candidate_id: str,
        attempt_number: int,
        schema_version: str,
        document_analysis_schema_version: str,
        prompt_version: str,
        skill_version: str,
        agent_version: str,
        retriever_version: str,
        model: str,
        started_at: datetime,
        processing_job_id: uuid.UUID | None = None,
        supersedes_mapping_run_id: uuid.UUID | None = None,
    ) -> SkuMappingRun:
        """Create one validated running attempt for one candidate without calling Claude.

        Resolves the external `candidate_id` (as `SkuMappingTask` carries it)
        to the internal `document_product_candidates.id` foreign key — the
        settled storage shape keeps the external string out of the run row
        itself. When `supersedes_mapping_run_id` is given, enforces that the
        referenced run shares this run's organization, document, analysis
        run, and candidate.
        """

        async with self._sessions() as session:
            async with session.begin():
                candidate = await self._validate_source(
                    session, organization_id, document_id, analysis_run_id, candidate_id,
                )
                if supersedes_mapping_run_id is not None:
                    await self._validate_supersedes(
                        session, supersedes_mapping_run_id, organization_id, document_id,
                        analysis_run_id, candidate.id,
                    )
                existing = await session.get(SkuMappingRun, run_id)
                if existing is not None:
                    if (
                        existing.organization_id == organization_id
                        and existing.document_id == document_id
                        and existing.analysis_run_id == analysis_run_id
                        and existing.document_product_candidate_id == candidate.id
                        and existing.attempt_number == attempt_number
                    ):
                        return existing
                    raise SkuMappingPersistenceError("mapping_run_conflict")
                duplicate = await session.scalar(
                    select(SkuMappingRun).where(
                        SkuMappingRun.document_product_candidate_id == candidate.id,
                        SkuMappingRun.attempt_number == attempt_number,
                    )
                )
                if duplicate is not None:
                    raise SkuMappingPersistenceError("mapping_attempt_exists")
                run = SkuMappingRun(
                    id=run_id,
                    organization_id=organization_id,
                    document_id=document_id,
                    analysis_run_id=analysis_run_id,
                    document_product_candidate_id=candidate.id,
                    processing_job_id=processing_job_id,
                    supersedes_mapping_run_id=supersedes_mapping_run_id,
                    attempt_number=attempt_number,
                    status="running",
                    schema_version=schema_version,
                    document_analysis_schema_version=document_analysis_schema_version,
                    prompt_version=prompt_version,
                    skill_version=skill_version,
                    agent_version=agent_version,
                    retriever_version=retriever_version,
                    model=model,
                    started_at=started_at,
                    validation_status="pending",
                )
                session.add(run)
                return run

    async def save_completed_result(
        self,
        *,
        run_id: uuid.UUID,
        artifact: SkuMappingRunArtifact,
        runtime: SkuMappingRuntimeSummary | None = None,
    ) -> tuple[str, str]:
        """Save a validated run artifact atomically, cleaning a new orphan artifact on failure."""

        if runtime is None:
            raise SkuMappingPersistenceError("mapping_completion_runtime_missing")
        if validate_sku_mapping_decision(artifact.task, artifact.decision, runtime):
            raise SkuMappingPersistenceError("mapping_completion_gate_failed")
        canonical = canonical_json_bytes(artifact)
        if len(canonical) > MAXIMUM_UNCOMPRESSED_RESULT_BYTES:
            raise SkuMappingPersistenceError("mapping_artifact_too_large")
        content_checksum = hashlib.sha256(canonical).hexdigest()

        async with self._sessions() as session:
            run = await session.get(SkuMappingRun, run_id)
            if run is None:
                raise SkuMappingPersistenceError("mapping_run_not_found")
            if run.status == "completed":
                if run.content_sha256_checksum == content_checksum and run.canonical_result_storage_key and run.compressed_sha256_checksum:
                    return run.canonical_result_storage_key, run.compressed_sha256_checksum
                raise SkuMappingPersistenceError("mapping_result_conflict")
            if run.status != "running":
                raise SkuMappingPersistenceError("mapping_run_not_running")

        compressed = compress_canonical_json(canonical)
        if len(compressed) > MAXIMUM_COMPRESSED_RESULT_BYTES:
            raise SkuMappingPersistenceError("mapping_artifact_too_large")
        key = self._artifact_key(artifact, run_id)

        async def chunks():
            yield compressed

        try:
            metadata = await self._storage.put(
                key, chunks(), maximum_bytes=MAXIMUM_COMPRESSED_RESULT_BYTES,
            )
        except StorageError:
            raise SkuMappingPersistenceError("mapping_artifact_storage_failed") from None

        try:
            async with self._sessions() as session:
                async with session.begin():
                    run = await session.scalar(
                        select(SkuMappingRun).where(SkuMappingRun.id == run_id).with_for_update()
                    )
                    if run is None:
                        raise SkuMappingPersistenceError("mapping_run_not_found")
                    if run.status == "completed":
                        if run.content_sha256_checksum == content_checksum and run.canonical_result_storage_key and run.compressed_sha256_checksum:
                            await self._storage.delete(key)
                            return run.canonical_result_storage_key, run.compressed_sha256_checksum
                        raise SkuMappingPersistenceError("mapping_result_conflict")
                    if run.status != "running":
                        raise SkuMappingPersistenceError("mapping_run_not_running")
                    sku = await self._resolve_sku(session, run, artifact.decision)
                    evidence = await self._resolve_evidence(session, run, artifact.decision)
                    await self._add_projections(session, run, artifact.decision, sku, evidence)
                    run.status = "completed"
                    run.validation_status = "validated"
                    run.catalog_version_id = artifact.decision.catalog_version_id
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
        run_id: uuid.UUID,
        *,
        error_code: str,
        error_message: str,
        terminal_reason: str | None = None,
        completed_at: datetime | None = None,
        runtime: SkuMappingRuntimeSummary | None = None,
    ) -> None:
        """Record only a bounded safe terminal failure and never overwrite completion."""

        async with self._sessions() as session:
            async with session.begin():
                run = await session.get(SkuMappingRun, run_id)
                if run is None:
                    raise SkuMappingPersistenceError("mapping_run_not_found")
                if run.status == "completed":
                    raise SkuMappingPersistenceError("mapping_run_already_completed")
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
        self, *, organization_id: uuid.UUID, run_id: uuid.UUID,
    ) -> SkuMappingRunArtifact:
        """Load, verify, decompress, and validate a completed artifact without Claude or PDFs."""

        async with self._sessions() as session:
            run = await session.scalar(
                select(SkuMappingRun).where(
                    SkuMappingRun.id == run_id,
                    SkuMappingRun.organization_id == organization_id,
                    SkuMappingRun.status == "completed",
                )
            )
        if (
            run is None
            or not run.canonical_result_storage_key
            or not run.compressed_sha256_checksum
            or not run.content_sha256_checksum
            or run.compression_method != RESULT_COMPRESSION
        ):
            raise SkuMappingPersistenceError("completed_mapping_result_unavailable")
        try:
            stored = await self._storage.read(
                run.canonical_result_storage_key, maximum_bytes=MAXIMUM_COMPRESSED_RESULT_BYTES,
            )
        except StorageError:
            raise SkuMappingPersistenceError("mapping_artifact_unavailable") from None
        if hashlib.sha256(stored).hexdigest() != run.compressed_sha256_checksum:
            raise SkuMappingPersistenceError("mapping_artifact_checksum_mismatch")
        if run.compressed_size is not None and len(stored) != run.compressed_size:
            raise SkuMappingPersistenceError("mapping_artifact_size_mismatch")
        try:
            canonical = decompress_canonical_json(stored, maximum_bytes=MAXIMUM_UNCOMPRESSED_RESULT_BYTES)
            artifact = SkuMappingRunArtifact.model_validate_json(canonical)
        except (ValueError, UnicodeDecodeError):
            raise SkuMappingPersistenceError("mapping_artifact_invalid") from None
        if (
            run.uncompressed_size is not None and len(canonical) != run.uncompressed_size
        ) or hashlib.sha256(canonical).hexdigest() != run.content_sha256_checksum:
            raise SkuMappingPersistenceError("mapping_artifact_checksum_mismatch")
        if (
            artifact.task.organization_id != run.organization_id
            or artifact.task.analysis_run_id != run.analysis_run_id
            or artifact.decision.catalog_version_id != run.catalog_version_id
        ):
            raise SkuMappingPersistenceError("mapping_artifact_identity_mismatch")
        return artifact

    async def latest_run(
        self, *, organization_id: uuid.UUID, document_product_candidate_id: uuid.UUID,
    ) -> SkuMappingRun | None:
        """Return one tenant-scoped latest run for one candidate without artifact internals."""

        async with self._sessions() as session:
            return await session.scalar(
                select(SkuMappingRun)
                .where(
                    SkuMappingRun.organization_id == organization_id,
                    SkuMappingRun.document_product_candidate_id == document_product_candidate_id,
                )
                .order_by(SkuMappingRun.created_at.desc(), SkuMappingRun.id.desc())
            )

    @staticmethod
    def _artifact_key(artifact: SkuMappingRunArtifact, run_id: uuid.UUID) -> str:
        """Construct the internal relative canonical-artifact key for one attempt."""
        return (
            f"organizations/{artifact.task.organization_id}/documents/{artifact.task.document_id}/"
            f"sku-mapping/{run_id}/sku-mapping-result.json.gz"
        )

    async def _validate_source(
        self, session: AsyncSession, organization_id: uuid.UUID, document_id: uuid.UUID,
        analysis_run_id: uuid.UUID, candidate_id: str,
    ) -> DocumentProductCandidate:
        """Require a tenant-owned completed analysis run and its named candidate."""

        if await session.get(Organization, organization_id) is None:
            raise SkuMappingPersistenceError("mapping_organization_not_found")
        document = await session.scalar(
            select(Document).where(Document.id == document_id, Document.organization_id == organization_id)
        )
        if document is None:
            raise SkuMappingPersistenceError("mapping_document_not_found")
        analysis_run = await session.scalar(
            select(DocumentAnalysisRun).where(
                DocumentAnalysisRun.id == analysis_run_id,
                DocumentAnalysisRun.organization_id == organization_id,
                DocumentAnalysisRun.document_id == document_id,
            )
        )
        if analysis_run is None:
            raise SkuMappingPersistenceError("mapping_analysis_run_not_found")
        if analysis_run.status != "completed":
            raise SkuMappingPersistenceError("mapping_analysis_run_not_completed")
        candidate = await session.scalar(
            select(DocumentProductCandidate).where(
                DocumentProductCandidate.analysis_run_id == analysis_run_id,
                DocumentProductCandidate.organization_id == organization_id,
                DocumentProductCandidate.external_candidate_id == candidate_id,
            )
        )
        if candidate is None:
            raise SkuMappingPersistenceError("mapping_candidate_not_found")
        return candidate

    @staticmethod
    async def _validate_supersedes(
        session: AsyncSession, supersedes_mapping_run_id: uuid.UUID, organization_id: uuid.UUID,
        document_id: uuid.UUID, analysis_run_id: uuid.UUID, document_product_candidate_id: uuid.UUID,
    ) -> None:
        """Require a superseded run to share this run's organization/document/analysis/candidate."""

        superseded = await session.get(SkuMappingRun, supersedes_mapping_run_id)
        if superseded is None:
            raise SkuMappingPersistenceError("mapping_superseded_run_not_found")
        if (
            superseded.organization_id != organization_id
            or superseded.document_id != document_id
            or superseded.analysis_run_id != analysis_run_id
            or superseded.document_product_candidate_id != document_product_candidate_id
        ):
            raise SkuMappingPersistenceError("mapping_supersedes_lineage_mismatch")

    async def _resolve_sku(self, session: AsyncSession, run: SkuMappingRun, decision: SkuMappingDecision) -> Sku | None:
        """Independently re-confirm a MATCH decision's SKU against the live catalog."""

        if decision.outcome is not SkuMappingOutcome.MATCH:
            return None
        sku = await session.scalar(
            select(Sku).where(
                Sku.id == decision.sku_id,
                Sku.organization_id == run.organization_id,
                Sku.catalog_version_id == decision.catalog_version_id,
                Sku.is_active.is_(True),
            )
        )
        if sku is None or sku.sku_code != decision.sku_code or sku.name != decision.sku_name:
            raise SkuMappingPersistenceError("mapping_sku_not_confirmed")
        return sku

    async def _resolve_evidence(
        self, session: AsyncSession, run: SkuMappingRun, decision: SkuMappingDecision,
    ) -> list[tuple[int, EvidenceReference, DocumentBlock | DocumentTable]]:
        """Resolve each decision evidence link to exactly one matching persisted projection."""

        resolved = []
        for order, reference in enumerate(decision.evidence):
            resolved.append((order, reference, await self._resolve_evidence_reference(session, run, reference)))
        return resolved

    async def _resolve_evidence_reference(
        self, session: AsyncSession, run: SkuMappingRun, reference: EvidenceReference,
    ) -> DocumentBlock | DocumentTable:
        """Validate one evidence record against its exact persisted page/source/geometry."""

        page = await session.scalar(
            select(DocumentPage).where(
                DocumentPage.processing_run_id == reference.preprocessing_run_id,
                DocumentPage.organization_id == run.organization_id,
                DocumentPage.page_number == reference.page_number,
            )
        )
        if page is None:
            raise SkuMappingPersistenceError("mapping_evidence_page_not_found")
        if reference.block_id is not None:
            block = await session.scalar(
                select(DocumentBlock).where(
                    DocumentBlock.processing_run_id == reference.preprocessing_run_id,
                    DocumentBlock.organization_id == run.organization_id,
                    DocumentBlock.document_page_id == page.id,
                    DocumentBlock.external_block_id == reference.block_id,
                )
            )
            if block is None or block.representation != reference.representation.value:
                raise SkuMappingPersistenceError("mapping_evidence_mismatch")
            if reference.extraction_source is not None and block.extraction_source != reference.extraction_source.value:
                raise SkuMappingPersistenceError("mapping_evidence_mismatch")
            self._validate_bounding_box(reference, block)
            return block
        table = await session.scalar(
            select(DocumentTable).where(
                DocumentTable.processing_run_id == reference.preprocessing_run_id,
                DocumentTable.organization_id == run.organization_id,
                DocumentTable.document_page_id == page.id,
                DocumentTable.external_table_id == reference.table_id,
            )
        )
        if table is None or reference.representation.value != "table" or reference.extraction_source is not None:
            raise SkuMappingPersistenceError("mapping_evidence_mismatch")
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
            raise SkuMappingPersistenceError("mapping_evidence_bounding_box_mismatch")

    @staticmethod
    async def _add_projections(
        session: AsyncSession, run: SkuMappingRun, decision: SkuMappingDecision, sku: Sku | None,
        evidence: list[tuple[int, EvidenceReference, DocumentBlock | DocumentTable]],
    ) -> None:
        """Insert the deterministic decision and evidence projections within one transaction."""

        now = datetime.now(UTC)
        session.add(SkuMappingDecisionProjection(
            sku_mapping_run_id=run.id,
            organization_id=run.organization_id,
            document_product_candidate_id=run.document_product_candidate_id,
            outcome=decision.outcome.value,
            sku_id=sku.id if sku else None,
            sku_code=decision.sku_code,
            sku_name=decision.sku_name,
            considered_sku_ids=[str(value) for value in decision.considered_sku_ids],
            rationale=decision.rationale,
            created_at=now,
        ))
        for order, reference, target in evidence:
            is_block = isinstance(target, DocumentBlock)
            session.add(SkuMappingEvidenceReference(
                sku_mapping_run_id=run.id,
                organization_id=run.organization_id,
                preprocessing_run_id=reference.preprocessing_run_id,
                page_number=reference.page_number,
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
                evidence_order=order,
                created_at=now,
            ))

    @staticmethod
    def _apply_runtime(run: SkuMappingRun, runtime: SkuMappingRuntimeSummary | None) -> None:
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
