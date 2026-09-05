"""Render one trusted PDF page with PyMuPDF and store the encoded full-page image.

Input is a trusted source, one-based page, bounded configuration, and safe output
key. Output is `PageRenderResult`; no text, layout, embedded images, or semantics
are extracted, and no local path or image bytes leave the component.
"""

import asyncio
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import pymupdf
from pydantic import ValidationError

from maximor.preprocessing.contracts import TrustedPdfSource, validate_storage_key
from maximor.preprocessing.errors import (
    EncryptedPdfError,
    InvalidPageNumberError,
    PageRenderingError,
    PdfOpenError,
    RenderStorageError,
    UnsafeRasterDimensionsError,
    UnsafeStorageKeyError,
    UnsupportedImageFormatError,
)
from maximor.preprocessing.schemas import PageNumber, PageRenderResult
from maximor.storage import ObjectStorage, StorageError

DEFAULT_RENDER_DPI = 200
DEFAULT_MAXIMUM_OUTPUT_BYTES = 25 * 1024 * 1024
DEFAULT_MAXIMUM_PIXEL_COUNT = 40_000_000
JPEG_QUALITY = 85


@dataclass(frozen=True)
class PageRenderingConfiguration:
    """Bound requested DPI, encoded byte size, and raster pixel count."""

    dpi: int = DEFAULT_RENDER_DPI
    media_type: Literal["image/png", "image/jpeg"] = "image/png"
    maximum_output_bytes: int = DEFAULT_MAXIMUM_OUTPUT_BYTES
    maximum_pixel_count: int = DEFAULT_MAXIMUM_PIXEL_COUNT

    def __post_init__(self) -> None:
        """Reject unsupported formats and non-positive or impractical limits."""

        if not 1 <= self.dpi <= 2400:
            raise ValueError("rendering DPI must be between 1 and 2400")
        if self.media_type not in {"image/png", "image/jpeg"}:
            raise UnsupportedImageFormatError
        if self.maximum_output_bytes < 1 or self.maximum_pixel_count < 1:
            raise ValueError("rendering safety limits must be positive")


class PageRenderer(Protocol):
    """Render and store one complete page while keeping local paths internal."""

    async def render(
        self,
        source: TrustedPdfSource,
        page_number: PageNumber,
        configuration: PageRenderingConfiguration,
        *,
        output_storage_key: str,
    ) -> PageRenderResult:
        """Return a safe stored-image reference and deterministic image metadata."""

        ...


@dataclass(frozen=True)
class _RenderedPage:
    encoded_bytes: bytes
    pixel_width: int
    pixel_height: int


class PymupdfPageRenderer:
    """Render complete pages with PyMuPDF and persist them through `ObjectStorage`.

    PNG and RGB JPEG are supported; annotations and normalized page rotation are
    visible. The class does not expose bytes/paths or extract individual images.
    """

    def __init__(
        self,
        storage: ObjectStorage,
        document_opener: Callable[..., Any] = pymupdf.open,
    ) -> None:
        """Receive replaceable storage and document-opening dependencies."""

        self._storage = storage
        self._document_opener = document_opener

    async def render(
        self,
        source: TrustedPdfSource,
        page_number: PageNumber,
        configuration: PageRenderingConfiguration,
        *,
        output_storage_key: str,
    ) -> PageRenderResult:
        """Render safely in a thread, store bytes, and return validated metadata."""

        if not isinstance(page_number, int) or page_number < 1:
            raise InvalidPageNumberError(int(page_number), "page_renderer")
        try:
            validate_storage_key(output_storage_key)
        except ValueError:
            raise UnsafeStorageKeyError from None

        rendered = await asyncio.to_thread(
            self._render_sync, source, page_number, configuration
        )
        if len(rendered.encoded_bytes) > configuration.maximum_output_bytes:
            raise UnsafeRasterDimensionsError(page_number)

        async def chunks() -> AsyncIterator[bytes]:
            yield rendered.encoded_bytes

        stored = False
        try:
            metadata = await self._storage.put(
                output_storage_key,
                chunks(),
                maximum_bytes=configuration.maximum_output_bytes,
            )
            stored = True
            if metadata.size != len(rendered.encoded_bytes):
                raise RenderStorageError(page_number)
            return PageRenderResult(
                page_number=page_number,
                storage_key=output_storage_key,
                media_type=configuration.media_type,
                pixel_width=rendered.pixel_width,
                pixel_height=rendered.pixel_height,
                dpi=configuration.dpi,
                sha256_checksum=metadata.sha256_checksum,
            )
        except RenderStorageError:
            if stored:
                await self._best_effort_delete(output_storage_key)
            raise
        except (StorageError, ValidationError):
            if stored:
                await self._best_effort_delete(output_storage_key)
            raise RenderStorageError(page_number) from None

    def _render_sync(
        self,
        source: TrustedPdfSource,
        page_number: int,
        configuration: PageRenderingConfiguration,
    ) -> _RenderedPage:
        try:
            document = self._document_opener(source.local_path)
        except (OSError, RuntimeError, ValueError, TypeError):
            raise PdfOpenError("page_renderer") from None

        try:
            with document:
                if getattr(document, "needs_pass", False):
                    raise EncryptedPdfError
                if page_number > int(document.page_count):
                    raise InvalidPageNumberError(page_number, "page_renderer")
                page = document.load_page(page_number - 1)
                estimated_width = math.ceil(page.rect.width * configuration.dpi / 72)
                estimated_height = math.ceil(page.rect.height * configuration.dpi / 72)
                if estimated_width * estimated_height > configuration.maximum_pixel_count:
                    raise UnsafeRasterDimensionsError(page_number)
                pixmap = page.get_pixmap(
                    dpi=configuration.dpi,
                    colorspace=pymupdf.csRGB,
                    alpha=False,
                    annots=True,
                )
                if pixmap.width * pixmap.height > configuration.maximum_pixel_count:
                    raise UnsafeRasterDimensionsError(page_number)
                if configuration.media_type == "image/png":
                    encoded = pixmap.tobytes("png")
                elif configuration.media_type == "image/jpeg":
                    encoded = pixmap.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
                else:
                    raise UnsupportedImageFormatError
                return _RenderedPage(encoded, pixmap.width, pixmap.height)
        except (
            EncryptedPdfError,
            InvalidPageNumberError,
            UnsafeRasterDimensionsError,
            UnsupportedImageFormatError,
        ):
            raise
        except Exception:
            raise PageRenderingError(page_number) from None

    async def _best_effort_delete(self, output_storage_key: str) -> None:
        try:
            await self._storage.delete(output_storage_key)
        except Exception:
            pass
