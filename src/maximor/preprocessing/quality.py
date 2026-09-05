"""Apply deterministic native-text quality rules for the OCR decision.

Input is native text, layout, and tables for one page. Output is a quality result;
no text extraction, OCR, spelling correction, or semantic judgment occurs here.
"""
import unicodedata
from typing import Protocol
from maximor.preprocessing.schemas import LayoutExtractionResult, NativeTextExtractionResult, OcrDecisionReason, PageQualityAssessment, TableExtractionResult

class PageQualityEvaluator(Protocol):
    """Evaluate deterministic signals deciding whether OCR is required."""
    def evaluate(self, native_text: NativeTextExtractionResult, layout: LayoutExtractionResult, tables: TableExtractionResult) -> PageQualityAssessment:
        """Return a page-consistent quality result without commercial meaning."""
        ...

class RuleBasedPageQualityEvaluator:
    """Use printable-character quality rules, not semantic or LLM scoring.

    The ratio is readable non-whitespace characters divided by all non-whitespace
    characters. Valid readable misspellings intentionally do not trigger OCR.
    """
    def __init__(self, minimum_readable_character_ratio: float = 0.90) -> None:
        """Set the minimum ratio at which native text is sufficient."""
        if not 0 <= minimum_readable_character_ratio <= 1:
            raise ValueError("minimum readable character ratio must be between zero and one")
        self._minimum = minimum_readable_character_ratio

    def evaluate(self, native_text: NativeTextExtractionResult, layout: LayoutExtractionResult, tables: TableExtractionResult) -> PageQualityAssessment:
        """Return deterministic signals after requiring all component pages to match."""
        if len({native_text.page_number, layout.page_number, tables.page_number}) != 1:
            raise ValueError("quality inputs must belong to the same page")
        chars = [char for char in native_text.plain_text if not char.isspace()]
        suspicious = any(char in {"\ufffd", "\x00"} or unicodedata.category(char) == "Cc" for char in chars)
        readable = sum(char not in {"\ufffd", "\x00"} and unicodedata.category(char) != "Cc" for char in chars)
        ratio = readable / len(chars) if chars else 0.0
        reasons=[]
        if not native_text.plain_text: reasons.append(OcrDecisionReason.EMPTY_NATIVE_TEXT)
        if ratio < self._minimum: reasons.append(OcrDecisionReason.LOW_READABLE_CHARACTER_RATIO)
        if suspicious: reasons.append(OcrDecisionReason.SUSPICIOUS_REPLACEMENT_CHARACTERS)
        if not reasons: reasons=[OcrDecisionReason.SUFFICIENT_NATIVE_TEXT]
        return PageQualityAssessment(page_number=native_text.page_number, native_character_count=native_text.character_count, readable_character_ratio=ratio, native_extraction_empty=not bool(native_text.plain_text), suspicious_replacement_characters_found=suspicious, ocr_required=reasons != [OcrDecisionReason.SUFFICIENT_NATIVE_TEXT], ocr_reasons=reasons)
