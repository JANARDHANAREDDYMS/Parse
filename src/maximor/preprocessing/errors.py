"""Define failures received from future deterministic components.

Errors output stable safe codes with optional page/component context. They do not
expose PDF bytes, local paths, stack traces, or implementation details.
"""


class PreprocessingError(Exception):
    """Carry a stable safe code/message and optional page/component attribution."""

    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        page_number: int | None = None,
        component_name: str | None = None,
    ) -> None:
        super().__init__(safe_message)
        if page_number is not None and page_number < 1:
            raise ValueError("error page_number must begin at one")
        self.code = code
        self.safe_message = safe_message
        self.page_number = page_number
        self.component_name = component_name


class InvalidPdfError(PreprocessingError):
    """Report a structurally invalid PDF without revealing its bytes or location."""
    def __init__(self) -> None:
        super().__init__("invalid_pdf", "The document is not a valid PDF.", component_name="pdf_inspector")


class EncryptedPdfError(PreprocessingError):
    """Report an encrypted PDF that cannot be processed safely."""
    def __init__(self) -> None:
        super().__init__("encrypted_pdf", "The encrypted PDF cannot be processed.", component_name="pdf_inspector")


class UnsupportedPdfError(PreprocessingError):
    """Report a valid PDF whose features are unsupported by the future processor."""
    def __init__(self) -> None:
        super().__init__("unsupported_pdf", "The PDF uses unsupported features.", component_name="pdf_inspector")


class PageExtractionError(PreprocessingError):
    """Report a deterministic per-page extraction failure."""
    def __init__(self, page_number: int, component_name: str) -> None:
        super().__init__("page_extraction_failed", "A page could not be extracted.",
                         page_number=page_number, component_name=component_name)


class PageRenderingError(PreprocessingError):
    """Report a full-page rendering or rendered-object storage failure."""
    def __init__(self, page_number: int) -> None:
        super().__init__("page_rendering_failed", "A page could not be rendered.",
                         page_number=page_number, component_name="page_renderer")


class OcrError(PreprocessingError):
    """Report an OCR engine failure while keeping native text untouched."""
    def __init__(self, page_number: int) -> None:
        super().__init__("ocr_failed", "OCR failed for the page.",
                         page_number=page_number, component_name="ocr")


class InvalidPreprocessingResultError(PreprocessingError):
    """Report final schema or cross-component validation failure."""
    def __init__(self) -> None:
        super().__init__("invalid_preprocessing_result", "The preprocessing result is invalid.",
                         component_name="document_preprocessor")


class InvalidPageNumberError(PreprocessingError):
    """Report a requested one-based page outside the source document."""

    def __init__(self, page_number: int, component_name: str) -> None:
        super().__init__(
            "invalid_page_number",
            "The requested page does not exist.",
            page_number=page_number if page_number >= 1 else None,
            component_name=component_name,
        )


class PdfOpenError(PreprocessingError):
    """Report a missing, malformed, encrypted, or unreadable PDF safely."""

    def __init__(self, component_name: str) -> None:
        super().__init__(
            "pdf_open_failed",
            "The PDF could not be opened for processing.",
            component_name=component_name,
        )


class UnsafeStorageKeyError(PreprocessingError):
    """Report an absolute or traversing output object key."""

    def __init__(self) -> None:
        super().__init__(
            "unsafe_output_storage_key",
            "The output storage key is unsafe.",
            component_name="page_renderer",
        )


class UnsafeRasterDimensionsError(PreprocessingError):
    """Report a render request exceeding configured pixel or dimension limits."""

    def __init__(self, page_number: int) -> None:
        super().__init__(
            "unsafe_raster_dimensions",
            "The requested page raster exceeds the configured safety limit.",
            page_number=page_number if page_number >= 1 else None,
            component_name="page_renderer",
        )


class UnsupportedImageFormatError(PreprocessingError):
    """Report a render media type outside PNG and JPEG."""

    def __init__(self) -> None:
        super().__init__(
            "unsupported_image_format",
            "The requested rendering format is unsupported.",
            component_name="page_renderer",
        )


class RenderStorageError(PreprocessingError):
    """Report inability to store or validate rendered page bytes."""

    def __init__(self, page_number: int) -> None:
        super().__init__(
            "render_storage_failed",
            "The rendered page could not be stored.",
            page_number=page_number,
            component_name="page_renderer",
        )


class LayoutExtractionError(PreprocessingError):
    """Report a complete physical layout extraction failure."""

    def __init__(self, page_number: int | None = None) -> None:
        super().__init__(
            "layout_extraction_failed",
            "The page layout could not be extracted.",
            page_number=page_number if page_number and page_number >= 1 else None,
            component_name="layout_extractor",
        )
