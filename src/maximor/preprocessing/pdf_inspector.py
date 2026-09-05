"""Specify inspection from a trusted PDF source to safe document facts.

Future inspection checks structure, encryption, pages, rotations, and metadata;
it outputs `PdfInspectionResult` and does not extract page content or semantics.
"""

import asyncio
import re
import warnings
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import ValidationError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from maximor.preprocessing.contracts import TrustedPdfSource
from maximor.preprocessing.errors import InvalidPdfError, InvalidPreprocessingResultError
from maximor.preprocessing.schemas import (
    PdfInspectionResult,
    PdfPageRotation,
    PreprocessingWarning,
    WarningSeverity,
)

SAFE_METADATA_FIELDS = {
    "/Title": "title",
    "/Author": "author",
    "/Subject": "subject",
    "/Creator": "creator",
    "/Producer": "producer",
    "/CreationDate": "creation_date",
    "/ModDate": "modification_date",
}


class PdfInspector(Protocol):
    """Inspect validity, encryption, page count, rotations, and bounded metadata."""
    async def inspect(self, source: TrustedPdfSource) -> PdfInspectionResult:
        """Return validated inspection facts without extracting text or semantics."""
        ...


class PypdfPdfInspector:
    """Inspect a `TrustedPdfSource` with pypdf and return safe structural metadata.

    It detects structure, version, encryption, page count, metadata, and rotation.
    It never extracts text/layout/tables, renders, performs OCR, persists, or logs paths.
    """

    def __init__(self, reader_factory: Callable[..., Any] = PdfReader) -> None:
        """Accept an injectable pypdf-compatible reader factory for isolated tests."""
        self._reader_factory = reader_factory

    async def inspect(self, source: TrustedPdfSource) -> PdfInspectionResult:
        """Open and inspect one trusted PDF, mapping unsafe failures to typed errors."""
        return await asyncio.to_thread(self._inspect_sync, source)

    def _inspect_sync(self, source: TrustedPdfSource) -> PdfInspectionResult:
        safe_warnings: list[PreprocessingWarning] = []
        try:
            with source.local_path.open("rb") as handle:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    reader = self._reader_factory(handle, strict=True)
                    pdf_version = self._pdf_version(reader)
                    is_encrypted = bool(reader.is_encrypted)
                    if is_encrypted and not reader.decrypt(""):
                        safe_warnings.append(self._warning(
                            "encrypted_pdf_password_required",
                            "The encrypted PDF requires a password and cannot be processed.",
                            WarningSeverity.ERROR,
                        ))
                        return PdfInspectionResult(
                            pdf_version=pdf_version,
                            page_count=None,
                            is_encrypted=True,
                            processing_permitted=False,
                            metadata={},
                            page_rotations=[],
                            warnings=safe_warnings,
                        )
                    if is_encrypted:
                        safe_warnings.append(self._warning(
                            "encrypted_pdf_empty_password",
                            "The encrypted PDF was readable using an empty password.",
                            WarningSeverity.INFORMATION,
                        ))

                    page_count = len(reader.pages)
                    if page_count == 0:
                        raise InvalidPdfError
                    metadata = self._safe_metadata(reader, safe_warnings)
                    rotations = [
                        PdfPageRotation(
                            page_number=index,
                            degrees=self._normalized_rotation(page, index, safe_warnings),
                        )
                        for index, page in enumerate(reader.pages, start=1)
                    ]
                    for _ in caught:
                        safe_warnings.append(self._warning(
                            "pdf_recoverable_warning",
                            "The PDF reader reported a recoverable structural issue.",
                            WarningSeverity.WARNING,
                        ))
                    return PdfInspectionResult(
                        pdf_version=pdf_version,
                        page_count=page_count,
                        is_encrypted=is_encrypted,
                        processing_permitted=True,
                        metadata=metadata,
                        page_rotations=rotations,
                        warnings=safe_warnings,
                    )
        except InvalidPdfError:
            raise
        except ValidationError as exc:
            raise InvalidPreprocessingResultError from exc
        except (OSError, PdfReadError, EOFError, ValueError, TypeError, KeyError):
            raise InvalidPdfError from None

    @staticmethod
    def _pdf_version(reader: Any) -> str | None:
        header = getattr(reader, "pdf_header", None)
        if not isinstance(header, str):
            return None
        match = re.fullmatch(r"%PDF-(\d+\.\d+)", header.strip())
        return match.group(1) if match else None

    def _safe_metadata(
        self, reader: Any, safe_warnings: list[PreprocessingWarning]
    ) -> dict[str, str]:
        try:
            source_metadata = reader.metadata or {}
        except (PdfReadError, ValueError, TypeError):
            safe_warnings.append(self._warning(
                "pdf_metadata_unreadable",
                "The PDF metadata could not be read safely.",
                WarningSeverity.WARNING,
            ))
            return {}

        result: dict[str, str] = {}
        for source_key, output_key in SAFE_METADATA_FIELDS.items():
            raw_value = source_metadata.get(source_key)
            if raw_value is None:
                continue
            value = str(raw_value).replace("\x00", "").strip()
            if not value:
                continue
            if self._looks_sensitive(value):
                safe_warnings.append(self._warning(
                    "pdf_metadata_value_omitted",
                    "A PDF metadata value was omitted because it was unsafe.",
                    WarningSeverity.WARNING,
                ))
                continue
            if len(value) > 1000:
                value = value[:1000]
                safe_warnings.append(self._warning(
                    "pdf_metadata_value_truncated",
                    "A PDF metadata value was truncated to the safe limit.",
                    WarningSeverity.WARNING,
                ))
            result[output_key] = value
        return result

    @staticmethod
    def _normalized_rotation(
        page: Any,
        page_number: int,
        safe_warnings: list[PreprocessingWarning],
    ) -> int:
        raw_rotation = page.get("/Rotate", 0)
        try:
            rotation = int(raw_rotation)
        except (TypeError, ValueError, OverflowError):
            rotation = 0
        normalized = rotation % 360
        if normalized not in {0, 90, 180, 270}:
            safe_warnings.append(PypdfPdfInspector._warning(
                "invalid_page_rotation",
                "An invalid page rotation was normalized to zero.",
                WarningSeverity.WARNING,
                page_number=page_number,
            ))
            return 0
        return normalized

    @staticmethod
    def _looks_sensitive(value: str) -> bool:
        return bool(
            "Traceback (most recent call last)" in value
            or "file://" in value.lower()
            or re.search(r"(?:^|\s)/(?:Users|home|tmp|private|var|etc)/", value)
            or re.search(r"(?:^|\s)[A-Za-z]:\\", value)
        )

    @staticmethod
    def _warning(
        code: str,
        message: str,
        severity: WarningSeverity,
        *,
        page_number: int | None = None,
    ) -> PreprocessingWarning:
        return PreprocessingWarning(
            code=code,
            severity=severity,
            message=message,
            page_number=page_number,
            component_name="pdf_inspector",
        )
