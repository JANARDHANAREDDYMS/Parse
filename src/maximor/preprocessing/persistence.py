"""Persist and reload losslessly compressed canonical preprocessing results."""

import gzip
import hashlib
import json
import uuid
from datetime import UTC, datetime
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maximor.db.models import (
    DocumentBlock,
    DocumentPage,
    DocumentProcessingRun,
    DocumentTable,
)
from maximor.preprocessing.schemas import PreprocessedDocument
from maximor.storage import ObjectStorage


RESULT_COMPRESSION = "gzip"
MAXIMUM_COMPRESSED_RESULT_BYTES = 50 * 1024 * 1024
MAXIMUM_UNCOMPRESSED_RESULT_BYTES = 100 * 1024 * 1024


def canonical_json_bytes(result: PreprocessedDocument) -> bytes:
    """Serialize every representation to deterministic compact UTF-8 JSON."""

    return json.dumps(
        result.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compress_canonical_json(payload: bytes) -> bytes:
    """Compress canonical JSON losslessly with a reproducible gzip timestamp."""

    return gzip.compress(payload, compresslevel=9, mtime=0)


def decompress_canonical_json(payload: bytes, *, maximum_bytes: int) -> bytes:
    """Decompress gzip while enforcing a limit on expanded JSON bytes."""

    if maximum_bytes < 1:
        raise ValueError("preprocessing result read limit is invalid")
    try:
        with gzip.GzipFile(fileobj=BytesIO(payload), mode="rb") as compressed:
            result = compressed.read(maximum_bytes + 1)
    except (gzip.BadGzipFile, EOFError, OSError):
        raise ValueError("preprocessing result compression is invalid") from None
    if len(result) > maximum_bytes:
        raise ValueError("preprocessing result exceeds the expanded size limit")
    return result


class PreprocessingResultRepository:
    """Store compressed snapshots plus consistent searchable projections."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
    ) -> None:
        self._sessions = session_factory
        self._storage = storage

    async def start_run(
        self,
        *,
        run_id: uuid.UUID,
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
        job_id: uuid.UUID,
        attempt: int,
        checksum: str,
        schema_version: str,
        processor_version: str,
        started_at: datetime,
    ) -> DocumentProcessingRun:
        async with self._sessions() as session:
            async with session.begin():
                run = await session.get(DocumentProcessingRun, run_id)
                if run:
                    return run
                run = DocumentProcessingRun(
                    id=run_id,
                    organization_id=organization_id,
                    document_id=document_id,
                    processing_job_id=job_id,
                    attempt_number=attempt,
                    status="running",
                    schema_version=schema_version,
                    processor_version=processor_version,
                    original_document_checksum=checksum,
                    started_at=started_at,
                )
                session.add(run)
                return run

    async def save_completed_result(
        self, result: PreprocessedDocument
    ) -> tuple[str, str]:
        """Store one complete compressed snapshot and its relational projections."""

        existing = await self._completed_artifact(result.preprocessing_run_id)
        if existing is not None:
            return existing

        key = (
            f"organizations/{result.organization_id}/documents/{result.document_id}/"
            f"preprocessing/{result.preprocessing_run_id}/preprocessed-document.json.gz"
        )
        canonical = canonical_json_bytes(result)
        if len(canonical) > MAXIMUM_UNCOMPRESSED_RESULT_BYTES:
            raise ValueError("preprocessing result exceeds the canonical size limit")
        compressed = compress_canonical_json(canonical)
        if len(compressed) > MAXIMUM_COMPRESSED_RESULT_BYTES:
            raise ValueError("preprocessing result exceeds the stored size limit")

        content_checksum = hashlib.sha256(canonical).hexdigest()

        async def chunks():
            yield compressed

        metadata = await self._storage.put(
            key,
            chunks(),
            maximum_bytes=MAXIMUM_COMPRESSED_RESULT_BYTES,
        )
        try:
            async with self._sessions() as session:
                async with session.begin():
                    run = await session.get(
                        DocumentProcessingRun, result.preprocessing_run_id
                    )
                    if run is None:
                        raise ValueError("preprocessing run is missing")
                    if run.status == "completed":
                        if run.result_storage_key and run.result_sha256_checksum:
                            await self._storage.delete(key)
                            return run.result_storage_key, run.result_sha256_checksum
                        raise ValueError("completed preprocessing run has no artifact")

                    run.status = "completed"
                    run.page_count = len(result.pages)
                    run.inspection = result.inspection.model_dump(mode="json")
                    run.warnings = [
                        warning.model_dump(mode="json") for warning in result.warnings
                    ]
                    run.result_storage_key = key
                    run.result_sha256_checksum = metadata.sha256_checksum
                    run.result_content_sha256_checksum = content_checksum
                    run.result_compression = RESULT_COMPRESSION
                    run.result_uncompressed_size = len(canonical)
                    run.result_compressed_size = metadata.size
                    run.completed_at = result.completed_at
                    await self._add_projections(session, result)
            return key, metadata.sha256_checksum
        except Exception:
            await self._storage.delete(key)
            raise

    async def _completed_artifact(self, run_id: uuid.UUID) -> tuple[str, str] | None:
        async with self._sessions() as session:
            run = await session.get(DocumentProcessingRun, run_id)
            if (
                run is not None
                and run.status == "completed"
                and run.result_storage_key
                and run.result_sha256_checksum
            ):
                return run.result_storage_key, run.result_sha256_checksum
        return None

    @staticmethod
    async def _add_projections(
        session: AsyncSession, result: PreprocessedDocument
    ) -> None:
        for page in result.pages:
            page_row = DocumentPage(
                processing_run_id=result.preprocessing_run_id,
                organization_id=result.organization_id,
                page_number=page.page_number,
                width_points=page.width_points,
                height_points=page.height_points,
                rotation_degrees=page.rotation_degrees,
                native_plain_text=page.native_text.plain_text,
                native_character_count=page.native_text.character_count,
                native_word_count=page.native_text.word_count,
                ocr_status=page.ocr.status.value if page.ocr else "not_required",
                ocr_plain_text=page.ocr.plain_text if page.ocr else None,
                quality=page.quality.model_dump(mode="json"),
                render_storage_key=page.render.storage_key,
                render_media_type=page.render.media_type,
                render_pixel_width=page.render.pixel_width,
                render_pixel_height=page.render.pixel_height,
                render_dpi=page.render.dpi,
                render_checksum=page.render.sha256_checksum,
                warnings=[warning.model_dump(mode="json") for warning in page.warnings],
                created_at=datetime.now(UTC),
            )
            session.add(page_row)
            await session.flush()

            for representation, blocks in (
                ("native_text", page.native_text.blocks),
                ("layout", page.layout.blocks),
                ("ocr", page.ocr.blocks if page.ocr else []),
            ):
                for block in blocks:
                    block_type = getattr(block, "block_type", None)
                    font = getattr(block, "font", None)
                    session.add(
                        DocumentBlock(
                            processing_run_id=result.preprocessing_run_id,
                            document_page_id=page_row.id,
                            organization_id=result.organization_id,
                            external_block_id=block.block_id,
                            representation=representation,
                            extraction_source=block.extraction_source.value,
                            block_type=block_type.value if block_type else None,
                            reading_order=block.reading_order,
                            text=block.text,
                            x0=block.bounding_box.x0,
                            y0=block.bounding_box.y0,
                            x1=block.bounding_box.x1,
                            y1=block.bounding_box.y1,
                            font=font.model_dump(mode="json") if font else None,
                            ocr_confidence=getattr(block, "confidence", None),
                            created_at=datetime.now(UTC),
                        )
                    )

            for table in page.tables.tables:
                session.add(
                    DocumentTable(
                        processing_run_id=result.preprocessing_run_id,
                        document_page_id=page_row.id,
                        organization_id=result.organization_id,
                        external_table_id=table.table_id,
                        table_index=table.table_index,
                        x0=table.bounding_box.x0,
                        y0=table.bounding_box.y0,
                        x1=table.bounding_box.x1,
                        y1=table.bounding_box.y1,
                        rows=[row.model_dump(mode="json") for row in table.rows],
                        warnings=[
                            warning.model_dump(mode="json")
                            for warning in table.warnings
                        ],
                        created_at=datetime.now(UTC),
                    )
                )

    async def mark_run_failed(self, run_id: uuid.UUID, code: str, message: str) -> None:
        async with self._sessions() as session:
            async with session.begin():
                run = await session.get(DocumentProcessingRun, run_id)
                if run:
                    run.status = "failed"
                    run.error_code = code[:100]
                    run.error_message = message
                    run.result_storage_key = None
                    run.result_sha256_checksum = None
                    run.result_content_sha256_checksum = None
                    run.result_compression = None
                    run.result_uncompressed_size = None
                    run.result_compressed_size = None
                    run.completed_at = datetime.now(UTC)

    async def latest_completed(
        self, organization_id: uuid.UUID, document_id: uuid.UUID
    ) -> DocumentProcessingRun | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(DocumentProcessingRun)
                .where(
                    DocumentProcessingRun.organization_id == organization_id,
                    DocumentProcessingRun.document_id == document_id,
                    DocumentProcessingRun.status == "completed",
                )
                .order_by(DocumentProcessingRun.completed_at.desc())
            )

    async def compress_legacy_artifact(
        self, organization_id: uuid.UUID, run_id: uuid.UUID
    ) -> tuple[str, str]:
        """Convert one verified legacy JSON artifact to gzip without reprocessing."""

        async with self._sessions() as session:
            run = await session.scalar(
                select(DocumentProcessingRun).where(
                    DocumentProcessingRun.id == run_id,
                    DocumentProcessingRun.organization_id == organization_id,
                    DocumentProcessingRun.status == "completed",
                )
            )
        if not run or not run.result_storage_key or not run.result_sha256_checksum:
            raise ValueError("completed preprocessing result is unavailable")
        if run.result_compression == RESULT_COMPRESSION:
            return run.result_storage_key, run.result_sha256_checksum
        if run.result_compression is not None:
            raise ValueError("preprocessing result compression is unsupported")

        original_key = run.result_storage_key
        original_checksum = run.result_sha256_checksum
        original = await self._storage.read(
            original_key, maximum_bytes=MAXIMUM_UNCOMPRESSED_RESULT_BYTES
        )
        if hashlib.sha256(original).hexdigest() != original_checksum:
            raise ValueError("preprocessing result checksum mismatch")
        validated = PreprocessedDocument.model_validate_json(original)
        canonical = canonical_json_bytes(validated)
        compressed = compress_canonical_json(canonical)
        if len(compressed) > MAXIMUM_COMPRESSED_RESULT_BYTES:
            raise ValueError("preprocessing result exceeds the stored size limit")

        new_key = (
            original_key[:-5] + ".json.gz"
            if original_key.endswith(".json")
            else original_key + ".gz"
        )

        async def chunks():
            yield compressed

        metadata = await self._storage.put(
            new_key,
            chunks(),
            maximum_bytes=MAXIMUM_COMPRESSED_RESULT_BYTES,
        )
        try:
            async with self._sessions() as session:
                async with session.begin():
                    current = await session.scalar(
                        select(DocumentProcessingRun)
                        .where(
                            DocumentProcessingRun.id == run_id,
                            DocumentProcessingRun.organization_id == organization_id,
                            DocumentProcessingRun.status == "completed",
                        )
                        .with_for_update()
                    )
                    if (
                        current is None
                        or current.result_storage_key != original_key
                        or current.result_sha256_checksum != original_checksum
                        or current.result_compression is not None
                    ):
                        raise ValueError("preprocessing result changed during migration")
                    current.result_storage_key = new_key
                    current.result_sha256_checksum = metadata.sha256_checksum
                    current.result_content_sha256_checksum = hashlib.sha256(
                        canonical
                    ).hexdigest()
                    current.result_compression = RESULT_COMPRESSION
                    current.result_uncompressed_size = len(canonical)
                    current.result_compressed_size = metadata.size
        except Exception:
            await self._storage.delete(new_key)
            raise

        if new_key != original_key:
            await self._storage.delete(original_key)
        return new_key, metadata.sha256_checksum

    async def load_preprocessed_document(
        self, organization_id: uuid.UUID, run_id: uuid.UUID
    ) -> PreprocessedDocument:
        """Verify, decompress, and validate a canonical result without its PDF."""

        async with self._sessions() as session:
            run = await session.scalar(
                select(DocumentProcessingRun).where(
                    DocumentProcessingRun.id == run_id,
                    DocumentProcessingRun.organization_id == organization_id,
                    DocumentProcessingRun.status == "completed",
                )
            )
        if not run or not run.result_storage_key or not run.result_sha256_checksum:
            raise ValueError("completed preprocessing result is unavailable")

        stored = await self._storage.read(
            run.result_storage_key,
            maximum_bytes=MAXIMUM_COMPRESSED_RESULT_BYTES,
        )
        if hashlib.sha256(stored).hexdigest() != run.result_sha256_checksum:
            raise ValueError("preprocessing result checksum mismatch")

        if run.result_compression is None:
            # Backward-compatible loading for artifacts written before migration 0004.
            canonical = stored
        elif run.result_compression == RESULT_COMPRESSION:
            if (
                run.result_compressed_size is not None
                and len(stored) != run.result_compressed_size
            ):
                raise ValueError("preprocessing result stored size mismatch")
            canonical = decompress_canonical_json(
                stored, maximum_bytes=MAXIMUM_UNCOMPRESSED_RESULT_BYTES
            )
            if (
                run.result_uncompressed_size is not None
                and len(canonical) != run.result_uncompressed_size
            ):
                raise ValueError("preprocessing result expanded size mismatch")
            if (
                run.result_content_sha256_checksum is None
                or hashlib.sha256(canonical).hexdigest()
                != run.result_content_sha256_checksum
            ):
                raise ValueError("preprocessing result content checksum mismatch")
        else:
            raise ValueError("preprocessing result compression is unsupported")

        return PreprocessedDocument.model_validate_json(canonical)
