"""Expose request and result contracts to future callers.

Imports output only Python types and perform no PDF extraction, file access,
database access, network access, persistence, or agent work.
"""

from maximor.preprocessing.contracts import DocumentPreprocessingRequest, TrustedPdfSource
from maximor.preprocessing.pdf_inspector import PdfInspector, PypdfPdfInspector
from maximor.preprocessing.schemas import PreprocessedDocument, PreprocessedPage
from maximor.preprocessing.service import DocumentPreprocessor

__all__ = [
    "DocumentPreprocessingRequest",
    "DocumentPreprocessor",
    "PreprocessedDocument",
    "PreprocessedPage",
    "PdfInspector",
    "PypdfPdfInspector",
    "TrustedPdfSource",
]
