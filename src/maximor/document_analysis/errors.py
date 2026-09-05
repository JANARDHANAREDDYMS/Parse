"""Define safe failures for the unconfigured document-analysis boundary.

These errors receive no document content. They report only stable safe codes and
messages; concrete analysis and tool execution remain intentionally unimplemented.
"""


class DocumentAnalysisError(Exception):
    """Carry a stable safe code and message for document-analysis callers."""

    def __init__(self, code: str, safe_message: str) -> None:
        """Create a safe error without paths, content, credentials, or tracebacks."""

        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class DocumentAnalysisNotConfiguredError(DocumentAnalysisError):
    """Report that no concrete document-analysis agent has been configured."""

    def __init__(self) -> None:
        """Create the explicit no-fake-success placeholder failure."""

        super().__init__(
            "document_analysis_not_configured",
            "Document analysis is not configured.",
        )


class DocumentToolError(DocumentAnalysisError):
    """Base class for safe persisted-document retrieval failures."""


class ProcessingRunNotFoundError(DocumentToolError):
    """Report a missing or cross-tenant preprocessing run."""
    def __init__(self) -> None: super().__init__("processing_run_not_found", "The preprocessing run is unavailable.")


class ProcessingRunNotCompletedError(DocumentToolError):
    """Report a preprocessing run that is not available for read-only retrieval."""
    def __init__(self) -> None: super().__init__("processing_run_not_completed", "The preprocessing run is not completed.")


class PageNotFoundError(DocumentToolError):
    """Report an unavailable page without database details."""
    def __init__(self) -> None: super().__init__("page_not_found", "The requested page is unavailable.")


class RepresentationUnavailableError(DocumentToolError):
    """Report an unavailable requested native or OCR representation."""
    def __init__(self) -> None: super().__init__("representation_unavailable", "The requested representation is unavailable.")


class EvidenceNotFoundError(DocumentToolError):
    """Report missing persisted block or table evidence."""
    def __init__(self) -> None: super().__init__("evidence_not_found", "The requested evidence is unavailable.")


class EvidenceMismatchError(DocumentToolError):
    """Report evidence inconsistent with its claimed persisted identity."""
    def __init__(self) -> None: super().__init__("evidence_mismatch", "The evidence reference is inconsistent.")


class RenderMissingError(DocumentToolError):
    """Report unavailable persisted page render safely."""
    def __init__(self) -> None: super().__init__("render_missing", "The page render is unavailable.")


class RenderIntegrityError(DocumentToolError):
    """Report a page-render checksum or metadata mismatch safely."""
    def __init__(self) -> None: super().__init__("render_integrity_failed", "The page render could not be validated.")
