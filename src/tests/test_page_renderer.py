"""Test PyMuPDF page rendering with generated PDFs and temporary object storage."""

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from maximor.preprocessing.contracts import TrustedPdfSource
from maximor.preprocessing.errors import (
    InvalidPageNumberError,
    RenderStorageError,
    UnsafeRasterDimensionsError,
    UnsafeStorageKeyError,
)
from maximor.preprocessing.page_renderer import (
    JPEG_QUALITY,
    PageRenderingConfiguration,
    PymupdfPageRenderer,
)
from maximor.storage import LocalObjectStorage, StorageError
from tests.preprocessing_pdf_factory import create_visual_pdf


def trusted(path: Path) -> TrustedPdfSource:
    """Wrap one generated PDF with a harmless relative logical key."""
    return TrustedPdfSource(path.resolve(), f"tests/{path.name}")


def stored_path(root: Path, key: str) -> Path:
    """Resolve a known test key beneath its temporary storage root."""
    return root.joinpath(*key.split("/"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media_type", "suffix", "expected_format"),
    [("image/png", "png", "PNG"), ("image/jpeg", "jpg", "JPEG")],
)
async def test_renders_complete_page_in_supported_formats(
    tmp_path, media_type, suffix, expected_format
):
    pdf_path = tmp_path / "visual.pdf"
    storage_root = tmp_path / "objects"
    create_visual_pdf(pdf_path, include_annotation=True)
    key = f"renders/run/page-1.{suffix}"
    result = await PymupdfPageRenderer(LocalObjectStorage(storage_root)).render(
        trusted(pdf_path), 1, PageRenderingConfiguration(dpi=144, media_type=media_type),
        output_storage_key=key,
    )
    encoded = stored_path(storage_root, key).read_bytes()
    with Image.open(stored_path(storage_root, key)) as image:
        assert image.format == expected_format
        assert image.mode == "RGB"
        assert image.getbbox() is not None
        assert image.getextrema() != ((255, 255), (255, 255), (255, 255))
    assert result.media_type == media_type
    assert result.pixel_width > 0 and result.pixel_height > 0
    assert result.sha256_checksum == hashlib.sha256(encoded).hexdigest()
    assert not result.storage_key.startswith("/")
    assert JPEG_QUALITY == 85


@pytest.mark.asyncio
async def test_one_based_page_selection_and_dpi_dimensions(tmp_path):
    pdf_path = tmp_path / "pages.pdf"
    create_visual_pdf(pdf_path, page_sizes=[(100, 200), (300, 400)])
    renderer = PymupdfPageRenderer(LocalObjectStorage(tmp_path / "objects"))
    first = await renderer.render(
        trusted(pdf_path), 1, PageRenderingConfiguration(dpi=72),
        output_storage_key="renders/first.png",
    )
    second = await renderer.render(
        trusted(pdf_path), 2, PageRenderingConfiguration(dpi=144),
        output_storage_key="renders/second.png",
    )
    assert (first.pixel_width, first.pixel_height) == (100, 200)
    assert (second.pixel_width, second.pixel_height) == (600, 800)


@pytest.mark.asyncio
async def test_rotated_page_dimensions_are_normalized(tmp_path):
    pdf_path = tmp_path / "rotated.pdf"
    create_visual_pdf(pdf_path, page_sizes=[(200, 300)], rotate_last_page=True)
    result = await PymupdfPageRenderer(LocalObjectStorage(tmp_path / "objects")).render(
        trusted(pdf_path), 1, PageRenderingConfiguration(dpi=72),
        output_storage_key="renders/rotated.png",
    )
    assert (result.pixel_width, result.pixel_height) == (300, 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("page_number", [0, 2])
async def test_invalid_page_number_is_typed_and_path_safe(tmp_path, page_number):
    pdf_path = tmp_path / "one.pdf"
    create_visual_pdf(pdf_path)
    with pytest.raises(InvalidPageNumberError) as captured:
        await PymupdfPageRenderer(LocalObjectStorage(tmp_path / "objects")).render(
            trusted(pdf_path), page_number, PageRenderingConfiguration(),
            output_storage_key="renders/page.png",
        )
    assert str(tmp_path) not in str(captured.value)


@pytest.mark.asyncio
async def test_unsafe_output_key_is_rejected(tmp_path):
    pdf_path = tmp_path / "one.pdf"
    create_visual_pdf(pdf_path)
    with pytest.raises(UnsafeStorageKeyError):
        await PymupdfPageRenderer(LocalObjectStorage(tmp_path / "objects")).render(
            trusted(pdf_path), 1, PageRenderingConfiguration(),
            output_storage_key="renders/../escape.png",
        )


@pytest.mark.asyncio
async def test_excessive_pixels_are_rejected_before_rasterization(tmp_path, monkeypatch):
    import pymupdf

    pdf_path = tmp_path / "large.pdf"
    create_visual_pdf(pdf_path, page_sizes=[(1000, 1000)])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("raster allocation occurred")

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", forbidden)
    with pytest.raises(UnsafeRasterDimensionsError):
        await PymupdfPageRenderer(LocalObjectStorage(tmp_path / "objects")).render(
            trusted(pdf_path), 1,
            PageRenderingConfiguration(dpi=200, maximum_pixel_count=100),
            output_storage_key="renders/page.png",
        )


class FailingStorage:
    """Reject all test writes without exposing storage implementation details."""

    async def put(self, *_args, **_kwargs):
        """Raise a safe simulated storage error."""
        raise StorageError("simulated", "Simulated storage failure.")

    async def delete(self, *_args, **_kwargs):
        """Accept best-effort cleanup."""


@pytest.mark.asyncio
async def test_storage_failure_becomes_typed_safe_error(tmp_path):
    pdf_path = tmp_path / "one.pdf"
    create_visual_pdf(pdf_path)
    with pytest.raises(RenderStorageError) as captured:
        await PymupdfPageRenderer(FailingStorage()).render(
            trusted(pdf_path), 1, PageRenderingConfiguration(),
            output_storage_key="renders/page.png",
        )
    assert str(tmp_path) not in str(captured.value)


@pytest.mark.asyncio
async def test_renderer_never_extracts_embedded_images(tmp_path, monkeypatch):
    import pymupdf

    pdf_path = tmp_path / "visual.pdf"
    create_visual_pdf(pdf_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("embedded image extraction was called")

    monkeypatch.setattr(pymupdf.Document, "extract_image", forbidden)
    result = await PymupdfPageRenderer(LocalObjectStorage(tmp_path / "objects")).render(
        trusted(pdf_path), 1, PageRenderingConfiguration(),
        output_storage_key="renders/page.png",
    )
    assert result.pixel_width > 0
