"""Define the future DocumentAnalysisAgent boundary and explicit unconfigured placeholder.

The agent receives an identifier-only request and returns a structured analysis
result. This placeholder invokes no tools, files, storage, databases, or Claude.
"""

from typing import Protocol

from maximor.document_analysis.contracts import DocumentAnalysisRequest, DocumentAnalysisToolset
from maximor.document_analysis.errors import DocumentAnalysisNotConfiguredError
from maximor.document_analysis.schemas import DocumentAnalysisResult


class DocumentAnalysisAgent(Protocol):
    """Analyze selected persisted preprocessing evidence through typed narrow tools."""

    async def analyze(
        self,
        request: DocumentAnalysisRequest,
        tools: DocumentAnalysisToolset,
    ) -> DocumentAnalysisResult:
        """Return a validated semantic result without performing SKU mapping."""

        ...


class UnconfiguredDocumentAnalysisAgent:
    """Fail explicitly until a concrete tool-using agent implementation is configured."""

    async def analyze(
        self,
        request: DocumentAnalysisRequest,
        tools: DocumentAnalysisToolset,
    ) -> DocumentAnalysisResult:
        """Raise a typed failure and never synthesize a fake successful analysis."""

        del request, tools
        raise DocumentAnalysisNotConfiguredError
