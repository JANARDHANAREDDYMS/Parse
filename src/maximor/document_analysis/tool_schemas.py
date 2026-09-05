"""Define bounded, read-only inputs and outputs for persisted document-analysis tools.

These schemas receive scoped identifiers and return selected persisted records. They
do not carry sessions, storage clients, paths, SQL, or Claude adapter payloads.
"""
import uuid
from typing import Literal
from pydantic import Field, model_validator
from maximor.document_analysis.schemas import AnalysisModel, EvidenceReference, EvidenceRepresentation
from maximor.preprocessing.schemas import BoundingBox, ExtractionSource

DEFAULT_LIMIT=20
MAXIMUM_LIMIT=100
MAXIMUM_RENDER_BYTES=25*1024*1024

class ToolScope(AnalysisModel):
    """Scope every read-only tool call to one tenant and preprocessing run."""
    organization_id: uuid.UUID
    preprocessing_run_id: uuid.UUID

class PageScopedInput(ToolScope):
    """Scope one read-only call to exactly one persisted page."""
    page_number: int=Field(ge=1)

class SearchDocumentInput(ToolScope):
    """Search bounded persisted text/table content without semantic interpretation."""
    query: str=Field(min_length=1,max_length=500)
    page_number: int|None=Field(default=None,ge=1)
    representations: tuple[EvidenceRepresentation,...]=(EvidenceRepresentation.NATIVE_TEXT,EvidenceRepresentation.OCR)
    limit: int=Field(default=DEFAULT_LIMIT,ge=1,le=MAXIMUM_LIMIT)
    @model_validator(mode='after')
    def usable(self):
        """Reject blank query values and empty representation selections."""
        if not self.query.strip() or not self.representations: raise ValueError('query and representations are required')
        return self

class GetPageTextInput(PageScopedInput):
    """Request exactly one explicit native or OCR text representation."""
    representation: Literal['native','ocr']
    limit: int=Field(default=MAXIMUM_LIMIT,ge=1,le=MAXIMUM_LIMIT)

class GetPageBlocksInput(PageScopedInput):
    """Request bounded blocks from exactly one representation."""
    representation: Literal['native','layout','ocr']
    block_type: str|None=Field(default=None,max_length=32)
    limit: int=Field(default=DEFAULT_LIMIT,ge=1,le=MAXIMUM_LIMIT)

class GetPageTablesInput(PageScopedInput):
    """Request bounded tables in ascending persisted table order."""
    limit: int=Field(default=DEFAULT_LIMIT,ge=1,le=MAXIMUM_LIMIT)

class GetPageRenderInput(PageScopedInput):
    """Request one validated stored render with a strict byte limit."""
    maximum_bytes: int=Field(default=MAXIMUM_RENDER_BYTES,ge=1,le=MAXIMUM_RENDER_BYTES)

class DocumentOverview(AnalysisModel):
    """Return compact run metadata and inventories without full page content."""
    organization_id: uuid.UUID; document_id: uuid.UUID; preprocessing_run_id: uuid.UUID
    schema_version: str; processor_version: str; page_count: int; warnings: tuple[dict,...]
    pages: tuple['PageInventory',...]

class PageInventory(AnalysisModel):
    """Summarize one page's representations, counts, render, and warnings."""
    page_number:int; native_text_available:bool; ocr_available:bool; ocr_status:str
    native_block_count:int; layout_block_count:int; ocr_block_count:int; table_count:int
    render_available:bool; warnings:tuple[dict,...]

class TextBlockResult(AnalysisModel):
    """Return one evidence-addressable text block from a selected representation."""
    block_id:str; page_number:int; representation:EvidenceRepresentation; extraction_source:ExtractionSource
    reading_order:int; text:str; bounding_box:BoundingBox

class BlockResult(TextBlockResult):
    """Return one physical persisted block without semantic correction."""
    block_type:str|None=None; font:dict|None=None; ocr_confidence:float|None=None

class TableResult(AnalysisModel):
    """Return one persisted table and its validated rows/cells without interpretation."""
    table_id:str; page_number:int; table_index:int; bounding_box:BoundingBox; rows:tuple[dict,...]; warnings:tuple[dict,...]

class SearchResult(AnalysisModel):
    """Return a deterministic persisted snippet paired with its evidence reference."""
    evidence:EvidenceReference; snippet:str

class PageRenderResult(AnalysisModel):
    """Return validated render bytes internally without a path or Claude encoding."""
    page_number:int; media_type:str; content:bytes; sha256_checksum:str; pixel_width:int; pixel_height:int; dpi:int

class EvidenceRegionInput(ToolScope):
    """Resolve exactly one validated persisted evidence reference."""
    evidence:EvidenceReference
    @model_validator(mode='after')
    def matching_run(self):
        """Reject cross-run evidence before database access."""
        if self.evidence.preprocessing_run_id!=self.preprocessing_run_id: raise ValueError('evidence preprocessing run must match tool scope')
        return self
