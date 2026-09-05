"""Expose versioned document-analysis contracts without invoking tools or an LLM.

Imports define schemas and the explicit placeholder agent only. They do not read
preprocessing data, access storage or databases, or make Claude calls.
"""

from maximor.document_analysis.agent import DocumentAnalysisAgent, UnconfiguredDocumentAnalysisAgent
from maximor.document_analysis.contracts import DocumentAnalysisRequest, DocumentAnalysisToolset
from maximor.document_analysis.schemas import DocumentAnalysisResult

__all__ = [
    "DocumentAnalysisAgent",
    "DocumentAnalysisRequest",
    "DocumentAnalysisResult",
    "DocumentAnalysisToolset",
    "UnconfiguredDocumentAnalysisAgent",
]
