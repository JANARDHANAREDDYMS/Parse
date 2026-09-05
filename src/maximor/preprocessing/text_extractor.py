"""Extract native line text from a trusted PDF page with PyMuPDF.

Input is a trusted source and one-based page. Output is native-only text; this
module does not OCR, correct text, or infer document meaning.
"""
import asyncio
import math
from typing import Any, Callable, Protocol
import pymupdf
from maximor.preprocessing.contracts import TrustedPdfSource
from maximor.preprocessing.errors import InvalidPageNumberError, PageExtractionError, PdfOpenError
from maximor.preprocessing.schemas import BoundingBox, FontMetadata, NativeTextBlock, NativeTextExtractionResult, PageNumber, PreprocessingWarning, WarningSeverity

class NativeTextExtractor(Protocol):
    """Extract embedded text while preserving its native provenance."""
    async def extract(self, source: TrustedPdfSource, page_number: PageNumber) -> NativeTextExtractionResult:
        """Return deterministic native lines without semantic interpretation."""
        ...

class PymupdfNativeTextExtractor:
    """Extract geometrically ordered native lines using PyMuPDF.

    Input is a trusted source/page and output is `NativeTextExtractionResult`.
    Mixed-font lines omit font metadata; OCR and semantic classification are excluded.
    """
    def __init__(self, document_opener: Callable[..., Any] = pymupdf.open) -> None:
        """Receive an injectable opener for tests."""
        self._document_opener = document_opener

    async def extract(self, source: TrustedPdfSource, page_number: PageNumber) -> NativeTextExtractionResult:
        """Run synchronous PyMuPDF extraction without blocking the worker loop."""
        if not isinstance(page_number, int) or page_number < 1:
            raise InvalidPageNumberError(int(page_number), "native_text_extractor")
        return await asyncio.to_thread(self._extract_sync, source, page_number)

    def _extract_sync(self, source: TrustedPdfSource, page_number: int) -> NativeTextExtractionResult:
        try:
            document = self._document_opener(source.local_path)
        except Exception:
            raise PdfOpenError("native_text_extractor") from None
        try:
            with document:
                if getattr(document, "needs_pass", False):
                    raise PdfOpenError("native_text_extractor")
                if page_number > document.page_count:
                    raise InvalidPageNumberError(page_number, "native_text_extractor")
                raw = document.load_page(page_number - 1).get_text("dict")
        except (PdfOpenError, InvalidPageNumberError):
            raise
        except Exception:
            raise PageExtractionError(page_number, "native_text_extractor") from None
        values: list[tuple[float, float, int, str, BoundingBox, FontMetadata | None]] = []
        ordinal = 0
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = "".join(str(span.get("text", "")) for span in spans).strip()
                if not text:
                    continue
                try:
                    x0, y0, x1, y1 = map(float, line["bbox"])
                    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
                        continue
                    box = BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)
                except Exception:
                    continue
                values.append((y0, x0, ordinal, text, box, self._uniform_font(spans)))
                ordinal += 1
        values.sort(key=lambda item: (item[0], item[1], item[2]))
        blocks = [NativeTextBlock(block_id=f"native:p{page_number:04d}:b{i:06d}", page_number=page_number, reading_order=i, text=value[3], bounding_box=value[4], font=value[5]) for i, value in enumerate(values)]
        plain_text = "\n".join(item.text for item in blocks)
        warnings = [] if blocks else [PreprocessingWarning(code="native_text_empty", severity=WarningSeverity.INFORMATION, message="The page has no embedded native text.", page_number=page_number, component_name="native_text_extractor")]
        return NativeTextExtractionResult(page_number=page_number, blocks=blocks, plain_text=plain_text, character_count=len(plain_text), word_count=len(plain_text.split()), warnings=warnings)

    @staticmethod
    def _uniform_font(spans: list[dict[str, Any]]) -> FontMetadata | None:
        """Return font data only for truly uniform non-empty spans."""
        items = [(span.get("font"), span.get("size"), span.get("flags")) for span in spans if span.get("text")]
        if not items or len(set(items)) != 1:
            return None
        family, size, flags = items[0]
        if not isinstance(family, str) or not family or not isinstance(size, (int, float)) or size <= 0:
            return None
        return FontMetadata(family=family, size_points=float(size), bold=bool(int(flags or 0) & 16), italic=bool(int(flags or 0) & 2))
