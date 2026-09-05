"""Validate preprocessing contracts using synthetic values without opening PDF files."""

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from maximor.preprocessing.contracts import DocumentPreprocessingRequest
from maximor.preprocessing.schemas import (
    BlockType, BoundingBox, ExtractedTable, ExtractionSource, LayoutBlock,
    LayoutExtractionResult, NativeTextBlock, NativeTextExtractionResult,
    OcrPageResult, OcrStatus, OcrTextBlock, PageQualityAssessment,
    PageRenderResult, PdfInspectionResult, PdfPageRotation, PreprocessedDocument,
    PreprocessedPage, TableExtractionResult, WarningSeverity,
)

CHECKSUM = "a" * 64


def box() -> BoundingBox:
    """Return one valid synthetic normalized PDF-point box."""
    return BoundingBox(x0=0, y0=0, x1=100, y1=40)


def native(page: int, block_id: str) -> NativeTextExtractionResult:
    """Return a synthetic one-block native text result."""
    return NativeTextExtractionResult(
        page_number=page,
        blocks=[NativeTextBlock(block_id=block_id, page_number=page,
                                reading_order=0, text="sample", bounding_box=box())],
        plain_text="sample", character_count=6, word_count=1,
    )


def layout(page: int, block_id: str) -> LayoutExtractionResult:
    """Return a synthetic one-block physical layout result."""
    return LayoutExtractionResult(
        page_number=page,
        width_points=612,
        height_points=792,
        blocks=[LayoutBlock(block_id=block_id, page_number=page, reading_order=0,
                            block_type=BlockType.TEXT, bounding_box=box(),
                            text="sample", extraction_source=ExtractionSource.NATIVE)],
    )


def page(page_number: int, suffix: str | None = None, **updates) -> PreprocessedPage:
    """Build one fully valid synthetic preprocessing page."""
    suffix = suffix or str(page_number)
    values = {
        "page_number": page_number, "width_points": 612, "height_points": 792,
        "rotation_degrees": 0,
        "native_text": native(page_number, f"native-{suffix}"),
        "layout": layout(page_number, f"layout-{suffix}"),
        "tables": TableExtractionResult(page_number=page_number, tables=[]),
        "render": PageRenderResult(page_number=page_number,
            storage_key=f"renders/run/page-{page_number}.png", media_type="image/png",
            pixel_width=1275, pixel_height=1650, dpi=150, sha256_checksum=CHECKSUM),
        "quality": PageQualityAssessment(page_number=page_number,
            native_character_count=6, readable_character_ratio=1,
            native_extraction_empty=False,
            suspicious_replacement_characters_found=False,
            ocr_required=False, ocr_reasons=["sufficient_native_text"]),
    }
    values.update(updates)
    return PreprocessedPage(**values)


def document(pages: list[PreprocessedPage], page_count: int | None = None, **updates):
    """Build a valid synthetic final document unless a test supplies invalid values."""
    started = datetime(2026, 1, 1, tzinfo=UTC)
    count = page_count if page_count is not None else len(pages)
    values = {
        "schema_version": "1.0", "organization_id": uuid.uuid4(),
        "document_id": uuid.uuid4(), "processing_job_id": uuid.uuid4(),
        "preprocessing_run_id": uuid.uuid4(), "processor_version": "contracts-1",
        "original_document_checksum": CHECKSUM,
        "inspection": PdfInspectionResult(pdf_version="1.7", page_count=count,
            is_encrypted=False, processing_permitted=True,
            page_rotations=[PdfPageRotation(page_number=i, degrees=0)
                            for i in range(1, count + 1)]),
        "pages": pages, "started_at": started, "completed_at": started + timedelta(seconds=1),
    }
    values.update(updates)
    return PreprocessedDocument(**values)


def test_valid_and_invalid_bounding_boxes():
    assert box().x1 == 100
    with pytest.raises(ValidationError):
        BoundingBox(x0=2, y0=0, x1=1, y1=1)


@pytest.mark.parametrize("coordinate", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_bounding_box_coordinates_are_rejected(coordinate):
    with pytest.raises(ValidationError):
        BoundingBox(x0=coordinate, y0=0, x1=1, y1=1)


def test_page_numbers_begin_at_one_and_reading_order_is_nonnegative():
    with pytest.raises(ValidationError):
        native(0, "block")
    with pytest.raises(ValidationError):
        NativeTextBlock(block_id="block", page_number=1, reading_order=-1,
                        text="x", bounding_box=box())


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan")])
def test_ocr_confidence_is_bounded_and_finite(confidence):
    with pytest.raises(ValidationError):
        OcrTextBlock(block_id="ocr", page_number=1, reading_order=0,
                     text="x", bounding_box=box(), confidence=confidence)


def test_ocr_status_must_agree_with_quality_decision():
    with pytest.raises(ValidationError):
        page(1, quality=PageQualityAssessment(page_number=1, native_character_count=0,
             readable_character_ratio=0, native_extraction_empty=True,
             suspicious_replacement_characters_found=False, ocr_required=True,
             ocr_reasons=["empty_native_text"]), ocr=None)
    with pytest.raises(ValidationError):
        OcrPageResult(page_number=1, status=OcrStatus.FAILED, blocks=[], plain_text="")


def test_nested_page_numbers_must_match():
    with pytest.raises(ValidationError):
        page(1, layout=layout(2, "layout-2"))


def test_duplicate_block_identifiers_are_rejected():
    with pytest.raises(ValidationError):
        page(1, layout=layout(1, "native-1"))


def test_duplicate_table_identifiers_are_rejected():
    table = ExtractedTable(table_id="table-1", page_number=1, bounding_box=box(),
                           table_index=0, rows=[])
    duplicate = table.model_copy(update={"table_index": 1})
    with pytest.raises(ValidationError):
        page(1, tables=TableExtractionResult(page_number=1, tables=[table, duplicate]))


def test_document_pages_are_sequential_and_count_matches_inspection():
    with pytest.raises(ValidationError):
        document([page(1), page(3)], page_count=2)
    with pytest.raises(ValidationError):
        document([page(1)], page_count=2)


def test_completion_cannot_precede_start():
    started = datetime(2026, 1, 2, tzinfo=UTC)
    with pytest.raises(ValidationError):
        document([page(1)], started_at=started, completed_at=started - timedelta(seconds=1))


@pytest.mark.parametrize("storage_key", ["/tmp/page.png", "renders/../page.png", r"renders\page.png"])
def test_unsafe_storage_keys_are_rejected(storage_key):
    with pytest.raises((ValidationError, ValueError)):
        PageRenderResult(page_number=1, storage_key=storage_key, media_type="image/png",
                         pixel_width=1, pixel_height=1, dpi=72, sha256_checksum=CHECKSUM)
    with pytest.raises(ValueError):
        DocumentPreprocessingRequest(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), storage_key)


def test_semantic_agent_fields_are_absent_and_forbidden():
    fields = PreprocessedDocument.model_fields
    assert not {"product_candidates", "sku_mappings", "commercial_status"} & fields.keys()
    payload = document([page(1)]).model_dump()
    payload["sku_mappings"] = []
    with pytest.raises(ValidationError):
        PreprocessedDocument.model_validate(payload)


def test_imports_have_no_extraction_or_infrastructure_side_effects(tmp_path):
    script = """
import sys
import maximor.preprocessing
import maximor.preprocessing.pdf_inspector
import maximor.preprocessing.text_extractor
import maximor.preprocessing.layout_extractor
import maximor.preprocessing.table_extractor
import maximor.preprocessing.page_renderer
import maximor.preprocessing.quality
import maximor.preprocessing.ocr
# pypdf may import Pillow internally; application code does not invoke it here.
for name in ('pdfplumber','fitz','pytesseract','asyncpg','claude_agent_sdk'):
    assert name not in sys.modules, name
"""
    environment = {"PYTHONPATH": os.path.abspath("src")}
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path,
                   env=environment, check=True, capture_output=True, text=True)
    assert list(tmp_path.iterdir()) == []
