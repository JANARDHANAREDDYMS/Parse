"""Expose document-analysis contracts and the injectable Claude agent boundary.

Imports perform no preprocessing-data access, storage access, database access, or
Claude call. A caller explicitly constructs and invokes the concrete agent.
"""

from maximor.document_analysis.agent import (
    ClaudeDocumentAnalysisAgent,
    DocumentAnalysisAgent,
    DocumentAnalysisExecution,
    DocumentAnalysisRuntimeSummary,
    UnconfiguredDocumentAnalysisAgent,
)
from maximor.document_analysis.contracts import DocumentAnalysisRequest, DocumentAnalysisToolset
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.document_analysis.schemas import ApplicabilityScope, DocumentAnalysisResult

__all__ = [
    "DocumentAnalysisAgent",
    "ClaudeDocumentAnalysisAgent",
    "DocumentAnalysisExecution",
    "DocumentAnalysisRequest",
    "DocumentAnalysisResult",
    "ApplicabilityScope",
    "DocumentAnalysisRuntimeSummary",
    "DocumentAnalysisPersistenceService",
    "DocumentAnalysisToolset",
    "UnconfiguredDocumentAnalysisAgent",
]
