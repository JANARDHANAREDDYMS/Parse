"""Orchestrate deterministic in-memory preprocessing in its fixed safe sequence.

Input is a request with a safe object key. Output is `PreprocessedDocument`; this
service neither persists results nor registers work with an API or worker.
"""
import asyncio
import hashlib
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol
from pydantic import ValidationError
from maximor.preprocessing.contracts import DocumentPreprocessingRequest, TrustedPdfSource, validate_storage_key
from maximor.preprocessing.errors import InvalidPreprocessingResultError
from maximor.preprocessing.layout_extractor import LayoutExtractor
from maximor.preprocessing.ocr import OcrExtractor
from maximor.preprocessing.page_renderer import PageRenderer, PageRenderingConfiguration
from maximor.preprocessing.pdf_inspector import PdfInspector
from maximor.preprocessing.quality import PageQualityEvaluator
from maximor.preprocessing.schemas import PreprocessedDocument, PreprocessedPage
from maximor.preprocessing.table_extractor import TableExtractor
from maximor.preprocessing.text_extractor import NativeTextExtractor
from maximor.storage import ObjectStorage, StorageError

PREPROCESSING_SCHEMA_VERSION = "1.0.0"
PREPROCESSOR_VERSION = "0.1.0"

class DocumentPreprocessor(Protocol):
    """Coordinate deterministic components into an in-memory document result."""
    async def preprocess(self, request: DocumentPreprocessingRequest) -> PreprocessedDocument:
        """Return validated output without persisting it or changing job state."""
        ...

class DeterministicDocumentPreprocessor:
    """Sequentially inspect, extract, render, assess, and conditionally OCR pages.

    It materializes only a verified safe-key object temporarily. Output retains page
    artifacts on success and deletes them on failure; persistence is excluded.
    """
    def __init__(self, storage: ObjectStorage, inspector: PdfInspector, native_text_extractor: NativeTextExtractor, layout_extractor: LayoutExtractor, table_extractor: TableExtractor, page_renderer: PageRenderer, quality_evaluator: PageQualityEvaluator, ocr_extractor: OcrExtractor, *, rendering_configuration: PageRenderingConfiguration = PageRenderingConfiguration(), maximum_input_bytes: int = 50 * 1024 * 1024, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        """Inject all component boundaries and bounded, deterministic configuration."""
        self._storage,self._inspector,self._native,self._layout,self._tables,self._renderer,self._quality,self._ocr=(storage,inspector,native_text_extractor,layout_extractor,table_extractor,page_renderer,quality_evaluator,ocr_extractor)
        self._rendering,self._maximum_input,self._now=rendering_configuration,maximum_input_bytes,now

    async def preprocess(self, request: DocumentPreprocessingRequest) -> PreprocessedDocument:
        """Execute the documented sequential pipeline and validate its final result."""
        started=self._utc(self._now()); run_id=request.preprocessing_run_id; artifacts=[]
        path: Path | None=None
        try:
            metadata=await self._storage.inspect(request.storage_key)
            data=await self._storage.read(request.storage_key, maximum_bytes=self._maximum_input)
            if len(data)!=metadata.size or hashlib.sha256(data).hexdigest()!=metadata.sha256_checksum:
                raise InvalidPreprocessingResultError
            path=await asyncio.to_thread(self._materialize, data)
            source=TrustedPdfSource(path, request.storage_key)
            inspection=await self._inspector.inspect(source)
            if not inspection.processing_permitted or inspection.page_count is None:
                raise InvalidPreprocessingResultError
            pages=[]
            suffix="png" if self._rendering.media_type=="image/png" else "jpg"
            for number in range(1,inspection.page_count+1):
                native=await self._native.extract(source,number)
                layout=await self._layout.extract(source,number)
                tables=await self._tables.extract(source,number)
                key=f"organizations/{request.organization_id}/documents/{request.document_id}/preprocessing/{run_id}/pages/page-{number:04d}.{suffix}"
                validate_storage_key(key)
                render=await self._renderer.render(source,number,self._rendering,output_storage_key=key)
                artifacts.append(key)
                quality=self._quality.evaluate(native,layout,tables)
                ocr=await self._ocr.extract(render,number) if quality.ocr_required else None
                pages.append(PreprocessedPage(page_number=number,width_points=layout.width_points,height_points=layout.height_points,rotation_degrees=next(item.degrees for item in inspection.page_rotations if item.page_number==number),native_text=native,layout=layout,tables=tables,render=render,quality=quality,ocr=ocr))
            try:
                return PreprocessedDocument(schema_version=PREPROCESSING_SCHEMA_VERSION,organization_id=request.organization_id,document_id=request.document_id,processing_job_id=request.processing_job_id,preprocessing_run_id=run_id,processor_version=PREPROCESSOR_VERSION,original_document_checksum=metadata.sha256_checksum,inspection=inspection,pages=pages,started_at=started,completed_at=self._utc(self._now()))
            except ValidationError:
                raise InvalidPreprocessingResultError from None
        except InvalidPreprocessingResultError:
            await self._cleanup(artifacts); raise
        except Exception:
            await self._cleanup(artifacts); raise
        finally:
            if path is not None: await asyncio.to_thread(path.unlink, missing_ok=True)

    @staticmethod
    def _materialize(data: bytes) -> Path:
        """Write verified bytes to a private temporary PDF path used only internally."""
        with tempfile.NamedTemporaryFile(prefix="maximor-preprocess-", suffix=".pdf", delete=False) as file:
            file.write(data)
            return Path(file.name)

    async def _cleanup(self, artifacts: list[str]) -> None:
        """Best-effort remove artifacts from an unsuccessful preprocessing attempt."""
        for key in artifacts:
            try: await self._storage.delete(key)
            except Exception: pass

    @staticmethod
    def _utc(value: datetime) -> datetime:
        """Normalize injected clock values to aware UTC timestamps."""
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
