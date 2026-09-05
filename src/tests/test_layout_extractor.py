"""Test pdfplumber physical layout extraction using generated temporary PDFs."""

from pathlib import Path

import pytest

from maximor.preprocessing.contracts import TrustedPdfSource
from maximor.preprocessing.errors import InvalidPageNumberError, LayoutExtractionError, PdfOpenError
from maximor.preprocessing.layout_extractor import PdfplumberLayoutExtractor, _Candidate
from maximor.preprocessing.schemas import BlockType, BoundingBox
from tests.preprocessing_pdf_factory import create_visual_pdf


def trusted(path: Path) -> TrustedPdfSource:
    """Wrap one generated PDF with a harmless relative logical key."""
    return TrustedPdfSource(path.resolve(), f"tests/{path.name}")


@pytest.mark.asyncio
async def test_extracts_text_image_and_drawing_regions_without_semantic_changes(tmp_path):
    path = tmp_path / "visual.pdf"
    create_visual_pdf(path)
    result = await PdfplumberLayoutExtractor().extract(trusted(path), 1)
    types = {block.block_type for block in result.blocks}
    assert {BlockType.TEXT, BlockType.IMAGE_REGION, BlockType.DRAWING_REGION} <= types
    assert [block.text for block in result.blocks if block.block_type == BlockType.TEXT] == [
        "Pr0duct SKU-1"
    ]
    assert all(block.page_number == 1 for block in result.blocks)
    assert all(not hasattr(block, "sku") and not hasattr(block, "commercial_status")
               for block in result.blocks)


@pytest.mark.asyncio
async def test_empty_page_returns_safe_warning(tmp_path):
    path = tmp_path / "empty.pdf"
    create_visual_pdf(path, include_content=False)
    result = await PdfplumberLayoutExtractor().extract(trusted(path), 1)
    assert result.blocks == []
    assert [warning.code for warning in result.warnings] == ["page_contains_no_layout_blocks"]


@pytest.mark.asyncio
async def test_reading_order_and_ids_are_sequential_and_deterministic(tmp_path):
    path = tmp_path / "visual.pdf"
    create_visual_pdf(path)
    extractor = PdfplumberLayoutExtractor()
    first = await extractor.extract(trusted(path), 1)
    second = await extractor.extract(trusted(path), 1)
    assert [block.reading_order for block in first.blocks] == list(range(len(first.blocks)))
    assert [block.block_id for block in first.blocks] == [
        f"layout:p0001:b{index:06d}" for index in range(len(first.blocks))
    ]
    assert first == second


def test_duplicate_regions_are_removed_deterministically():
    extractor = PdfplumberLayoutExtractor()
    candidate = _Candidate(
        block_type=BlockType.DRAWING_REGION,
        bounding_box=BoundingBox(x0=10, y0=20, x1=30, y1=40),
        text=None,
        original_position=0,
    )
    duplicate = _Candidate(
        block_type=BlockType.DRAWING_REGION,
        bounding_box=BoundingBox(x0=10, y0=20, x1=30, y1=40),
        text=None,
        original_position=1,
    )
    warnings = []
    retained = extractor._deduplicate([candidate, duplicate], warnings, 1)
    assert retained == [candidate]
    assert [warning.code for warning in warnings] == [
        "duplicate_layout_region_removed"
    ]


@pytest.mark.asyncio
async def test_bounding_boxes_are_top_left_and_inside_page(tmp_path):
    path = tmp_path / "visual.pdf"
    create_visual_pdf(path)
    result = await PdfplumberLayoutExtractor().extract(trusted(path), 1)
    text = next(block for block in result.blocks if block.block_type == BlockType.TEXT)
    assert 20 <= text.bounding_box.x0 <= text.bounding_box.x1 <= 200
    assert 0 <= text.bounding_box.y0 < text.bounding_box.y1 <= 300


@pytest.mark.asyncio
async def test_rotated_page_coordinates_use_normalized_bounds(tmp_path):
    path = tmp_path / "rotated.pdf"
    create_visual_pdf(path, page_sizes=[(200, 300)], rotate_last_page=True)
    result = await PdfplumberLayoutExtractor().extract(trusted(path), 1)
    assert result.blocks
    assert all(0 <= block.bounding_box.x0 <= block.bounding_box.x1 <= 300
               and 0 <= block.bounding_box.y0 <= block.bounding_box.y1 <= 200
               for block in result.blocks)


@pytest.mark.asyncio
@pytest.mark.parametrize("page_number", [0, 2])
async def test_invalid_page_numbers_raise_typed_errors(tmp_path, page_number):
    path = tmp_path / "one.pdf"
    create_visual_pdf(path)
    with pytest.raises(InvalidPageNumberError):
        await PdfplumberLayoutExtractor().extract(trusted(path), page_number)


@pytest.mark.asyncio
async def test_malformed_pdf_raises_typed_path_safe_error(tmp_path):
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"not a PDF")
    with pytest.raises((PdfOpenError, LayoutExtractionError)) as captured:
        await PdfplumberLayoutExtractor().extract(trusted(path), 1)
    assert str(path.resolve()) not in str(captured.value)
