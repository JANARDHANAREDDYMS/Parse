"""Define identifier-only analysis input and typed narrow read-only tool interfaces.

Requests identify persisted preprocessing output. The protocol returns selected data;
it never loads source PDFs, exposes database sessions, or invokes Claude.
"""
import uuid
from typing import TYPE_CHECKING, Protocol
from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from maximor.document_analysis.tool_schemas import (
        BlockResult, DocumentOverview, EvidenceRegionInput, GetPageBlocksInput, GetPageRenderInput,
        GetPageTablesInput, GetPageTextInput, PageRenderResult, SearchDocumentInput,
        SearchResult, TableResult, TextBlockResult, ToolScope,
    )

class DocumentAnalysisRequest(BaseModel):
    """Identify one validated preprocessing run and the versions governing analysis."""
    model_config=ConfigDict(extra='forbid',frozen=True)
    organization_id:uuid.UUID; document_id:uuid.UUID; preprocessing_run_id:uuid.UUID
    preprocessing_schema_version:str=Field(min_length=1,max_length=50)
    document_analysis_schema_version:str=Field(min_length=1,max_length=50)
    prompt_version:str=Field(min_length=1,max_length=100); agent_version:str=Field(min_length=1,max_length=100)
    @field_validator('preprocessing_schema_version','document_analysis_schema_version','prompt_version','agent_version')
    @classmethod
    def version_is_not_blank(cls,value:str)->str:
        """Reject whitespace-only version labels before future tool calls."""
        if not value.strip(): raise ValueError('version values must not be blank')
        return value

class DocumentAnalysisToolset(Protocol):
    """Expose only typed, tenant-scoped, bounded persisted-document retrieval."""
    async def get_document_overview(self, scope: "ToolScope") -> "DocumentOverview": ...
    async def search_document(self, request: "SearchDocumentInput") -> tuple["SearchResult", ...]: ...
    async def get_page_text(self, request: "GetPageTextInput") -> tuple["TextBlockResult", ...]: ...
    async def get_page_blocks(self, request: "GetPageBlocksInput") -> tuple["BlockResult", ...]: ...
    async def get_page_tables(self, request: "GetPageTablesInput") -> tuple["TableResult", ...]: ...
    async def get_page_render(self, request: "GetPageRenderInput") -> "PageRenderResult": ...
    async def get_evidence_region(self, request: "EvidenceRegionInput") -> "BlockResult | TableResult": ...
