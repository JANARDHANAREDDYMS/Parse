"""Extract positioned physical regions from one trusted PDF page with pdfplumber.

Input is a trusted source and one-based page number. Output is a deterministic
`LayoutExtractionResult`; no semantic classification, text correction, embedded
image-byte extraction, persistence, rendering, OCR, or agent work is performed.
"""

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from maximor.preprocessing.contracts import TrustedPdfSource
from maximor.preprocessing.errors import InvalidPageNumberError, LayoutExtractionError, PdfOpenError
from maximor.preprocessing.schemas import (
    BlockType, BoundingBox, ExtractionSource, LayoutBlock, LayoutExtractionResult,
    PageNumber, PreprocessingWarning, WarningSeverity,
)

DEFAULT_COORDINATE_TOLERANCE = 1e-6
DEFAULT_MAXIMUM_LAYOUT_BLOCKS = 10_000
_TYPE_PRIORITY = {
    BlockType.TEXT: 0, BlockType.IMAGE_REGION: 1,
    BlockType.DRAWING_REGION: 2, BlockType.UNKNOWN: 3,
}


class LayoutExtractor(Protocol):
    """Extract text, image, drawing, and unknown physical geometry from one page."""

    async def extract(self, source: TrustedPdfSource, page_number: PageNumber) -> LayoutExtractionResult:
        """Return positioned blocks without product, pricing, or contract meaning."""
        ...


@dataclass(frozen=True)
class _Candidate:
    block_type: BlockType
    bounding_box: BoundingBox
    text: str | None
    original_position: int


class PdfplumberLayoutExtractor:
    """Extract line-level text and visible region geometry with pdfplumber.

    Coordinates are normalized top-left PDF points. The component neither repairs
    text nor labels products, pricing, terms, signatures, SKUs, or commercial state.
    """

    def __init__(self, pdf_opener: Callable[..., Any] | None = None, *,
                 coordinate_tolerance: float = DEFAULT_COORDINATE_TOLERANCE,
                 maximum_blocks: int = DEFAULT_MAXIMUM_LAYOUT_BLOCKS) -> None:
        """Receive an injectable PDF opener and bounded geometry controls."""
        if not math.isfinite(coordinate_tolerance) or coordinate_tolerance < 0:
            raise ValueError("coordinate tolerance must be finite and non-negative")
        if maximum_blocks < 1:
            raise ValueError("maximum layout blocks must be positive")
        if pdf_opener is None:
            from pdfplumber import open as pdfplumber_open
            pdf_opener = pdfplumber_open
        self._pdf_opener = pdf_opener
        self._coordinate_tolerance = coordinate_tolerance
        self._maximum_blocks = maximum_blocks

    async def extract(self, source: TrustedPdfSource, page_number: PageNumber) -> LayoutExtractionResult:
        """Run synchronous pdfplumber work in a thread and return validated blocks."""
        if not isinstance(page_number, int) or page_number < 1:
            raise InvalidPageNumberError(int(page_number), "layout_extractor")
        return await asyncio.to_thread(self._extract_sync, source, page_number)

    def _extract_sync(self, source: TrustedPdfSource, page_number: int) -> LayoutExtractionResult:
        try:
            handle = source.local_path.open("rb")
        except OSError:
            raise PdfOpenError("layout_extractor") from None
        try:
            try:
                pdf_context = self._pdf_opener(handle)
            except Exception:
                raise PdfOpenError("layout_extractor") from None
            with pdf_context as pdf:
                if not getattr(pdf, "pages", None):
                    raise LayoutExtractionError(page_number)
                if page_number > len(pdf.pages):
                    raise InvalidPageNumberError(page_number, "layout_extractor")
                return self._extract_page(pdf.pages[page_number - 1], page_number)
        finally:
            handle.close()

    def _extract_page(self, page: Any, page_number: int) -> LayoutExtractionResult:
        warnings: list[PreprocessingWarning] = []
        candidates: list[_Candidate] = []
        position = 0
        try:
            for line in page.extract_text_lines(strip=True, return_chars=False) or []:
                text = str(line.get("text", "")).strip()
                if text:
                    position = self._append(candidates, warnings, BlockType.TEXT, line,
                                            text, page, page_number, position)
            for block_type, elements in (
                (BlockType.IMAGE_REGION, page.images or []),
                (BlockType.DRAWING_REGION, page.rects or []),
                (BlockType.DRAWING_REGION, page.lines or []),
                (BlockType.DRAWING_REGION, page.curves or []),
            ):
                for element in elements:
                    position = self._append(candidates, warnings, block_type, element,
                                            None, page, page_number, position)
        except Exception:
            raise LayoutExtractionError(page_number) from None

        candidates = self._deduplicate(candidates, warnings, page_number)
        candidates.sort(key=lambda item: (
            item.bounding_box.y0, item.bounding_box.x0,
            _TYPE_PRIORITY[item.block_type], item.original_position,
        ))
        if len(candidates) > self._maximum_blocks:
            candidates = candidates[:self._maximum_blocks]
            warnings.append(self._warning(
                "layout_partially_extracted",
                "Layout blocks exceeded the safe limit and were truncated.",
                page_number, WarningSeverity.WARNING,
            ))
        if not candidates:
            warnings.append(self._warning(
                "page_contains_no_layout_blocks",
                "The page contains no retained layout blocks.",
                page_number, WarningSeverity.INFORMATION,
            ))
        blocks = [LayoutBlock(
            block_id=f"layout:p{page_number:04d}:b{index:06d}",
            page_number=page_number, reading_order=index,
            block_type=item.block_type, bounding_box=item.bounding_box,
            text=item.text, extraction_source=ExtractionSource.NATIVE,
        ) for index, item in enumerate(candidates)]
        try:
            return LayoutExtractionResult(
                page_number=page_number,
                width_points=float(page.width),
                height_points=float(page.height),
                blocks=blocks,
                warnings=warnings,
            )
        except ValidationError:
            raise LayoutExtractionError(page_number) from None

    def _append(self, candidates: list[_Candidate], warnings: list[PreprocessingWarning],
                block_type: BlockType, element: dict[str, Any], text: str | None,
                page: Any, page_number: int, position: int) -> int:
        bounding_box = self._bounding_box(element, float(page.width), float(page.height))
        if bounding_box is None:
            warnings.append(self._warning(
                "invalid_layout_geometry_skipped",
                "A layout element with invalid geometry was skipped.",
                page_number, WarningSeverity.WARNING,
            ))
        else:
            candidates.append(_Candidate(block_type, bounding_box, text, position))
        return position + 1

    def _bounding_box(self, element: dict[str, Any], page_width: float,
                      page_height: float) -> BoundingBox | None:
        try:
            x0, y0, x1, y1 = (float(element["x0"]), float(element["top"]),
                              float(element["x1"]), float(element["bottom"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
            return None
        tolerance = self._coordinate_tolerance
        if (x0 < -tolerance or y0 < -tolerance or x1 > page_width + tolerance
                or y1 > page_height + tolerance or x1 < x0 - tolerance
                or y1 < y0 - tolerance):
            return None
        x0, x1 = min(max(x0, 0.0), page_width), min(max(x1, 0.0), page_width)
        y0, y1 = min(max(y0, 0.0), page_height), min(max(y1, 0.0), page_height)
        try:
            return BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)
        except ValidationError:
            return None

    def _deduplicate(self, candidates: list[_Candidate], warnings: list[PreprocessingWarning],
                     page_number: int) -> list[_Candidate]:
        retained: list[_Candidate] = []
        seen: set[tuple[Any, ...]] = set()
        removed = False
        for candidate in candidates:
            box = candidate.bounding_box
            key = (candidate.block_type.value, round(box.x0, 6), round(box.y0, 6),
                   round(box.x1, 6), round(box.y1, 6), candidate.text)
            if key in seen:
                removed = True
                continue
            seen.add(key)
            retained.append(candidate)
        if removed:
            warnings.append(self._warning(
                "duplicate_layout_region_removed",
                "A duplicate physical layout region was removed.",
                page_number, WarningSeverity.INFORMATION,
            ))
        return retained

    @staticmethod
    def _warning(code: str, message: str, page_number: int,
                 severity: WarningSeverity) -> PreprocessingWarning:
        return PreprocessingWarning(
            code=code, severity=severity, message=message,
            page_number=page_number, component_name="layout_extractor",
        )
