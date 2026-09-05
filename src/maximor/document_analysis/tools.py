"""Orchestrate bounded read-only persisted preprocessing retrieval for future agents.

Inputs are typed scopes. Outputs are compact typed tool results; this service does
not interpret content, access source PDFs, execute arbitrary SQL, or call Claude.
"""
import hashlib
from collections import Counter
from maximor.document_analysis.errors import EvidenceMismatchError, EvidenceNotFoundError, PageNotFoundError, ProcessingRunNotCompletedError, ProcessingRunNotFoundError, RenderIntegrityError, RenderMissingError, RepresentationUnavailableError
from maximor.document_analysis.repository import DocumentAnalysisRepository
from maximor.document_analysis.schemas import EvidenceReference, EvidenceRepresentation
from maximor.document_analysis.tool_schemas import (BlockResult, DocumentOverview, EvidenceRegionInput, GetPageBlocksInput, GetPageRenderInput, GetPageTablesInput, GetPageTextInput, PageInventory, PageRenderResult, SearchDocumentInput, SearchResult, TableResult, TextBlockResult, ToolScope)
from maximor.preprocessing.schemas import BoundingBox, ExtractionSource
from maximor.storage import ObjectStorage, StorageError

class PersistedDocumentTools:
    """Implement seven narrow read-only tools over completed tenant-scoped preprocessing runs."""
    def __init__(self, repository:DocumentAnalysisRepository, storage:ObjectStorage) -> None:
        """Receive repository and storage dependencies hidden behind typed tool methods."""
        self._repo,self._storage=repository,storage
    async def _run(self,scope:ToolScope):
        run=await self._repo.run(scope.organization_id,scope.preprocessing_run_id)
        if run is None: raise ProcessingRunNotFoundError
        if run.status!='completed': raise ProcessingRunNotCompletedError
        return run
    @staticmethod
    def _box(row): return BoundingBox(x0=row.x0,y0=row.y0,x1=row.x1,y1=row.y1)
    @staticmethod
    def _rep(value:str): return EvidenceRepresentation(value)
    def _block(self,row):
        return BlockResult(block_id=row.external_block_id,page_number=row.document_page_id and 0 or 0,representation=self._rep(row.representation),extraction_source=ExtractionSource(row.extraction_source),reading_order=row.reading_order,text=row.text or '',bounding_box=self._box(row),block_type=row.block_type,font=row.font,ocr_confidence=row.ocr_confidence)
    async def get_document_overview(self, scope:ToolScope)->DocumentOverview:
        """Return compact page inventory without text, blocks, tables, or render bytes."""
        run=await self._run(scope); pages=await self._repo.pages(scope.organization_id,scope.preprocessing_run_id); inventory=[]
        for page in pages:
            blocks=await self._repo.blocks(scope.organization_id,scope.preprocessing_run_id,page.page_number,'native_text',100)
            layout=await self._repo.blocks(scope.organization_id,scope.preprocessing_run_id,page.page_number,'layout',100)
            ocr=await self._repo.blocks(scope.organization_id,scope.preprocessing_run_id,page.page_number,'ocr',100)
            tables=await self._repo.tables(scope.organization_id,scope.preprocessing_run_id,page.page_number,100)
            inventory.append(PageInventory(page_number=page.page_number,native_text_available=bool(page.native_plain_text),ocr_available=page.ocr_status=='completed',ocr_status=page.ocr_status,native_block_count=len(blocks),layout_block_count=len(layout),ocr_block_count=len(ocr),table_count=len(tables),render_available=bool(page.render_storage_key),warnings=tuple(page.warnings or [])))
        return DocumentOverview(organization_id=scope.organization_id,document_id=run.document_id,preprocessing_run_id=scope.preprocessing_run_id,schema_version=run.schema_version,processor_version=run.processor_version,page_count=run.page_count or len(pages),warnings=tuple(run.warnings or []),pages=tuple(inventory))
    async def _page(self,request):
        await self._run(request); page=await self._repo.page(request.organization_id,request.preprocessing_run_id,request.page_number)
        if page is None: raise PageNotFoundError
        return page
    async def get_page_text(self,request:GetPageTextInput)->tuple[TextBlockResult,...]:
        """Return bounded text blocks for exactly native or OCR, never a merged representation."""
        await self._page(request); representation='native_text' if request.representation=='native' else 'ocr'
        rows=await self._repo.blocks(request.organization_id,request.preprocessing_run_id,request.page_number,representation,request.limit)
        if request.representation=='ocr' and not rows: raise RepresentationUnavailableError
        return tuple(TextBlockResult(block_id=row.external_block_id,page_number=request.page_number,representation=self._rep(row.representation),extraction_source=ExtractionSource(row.extraction_source),reading_order=row.reading_order,text=row.text or '',bounding_box=self._box(row)) for row in rows)
    async def get_page_blocks(self,request:GetPageBlocksInput)->tuple[BlockResult,...]:
        """Return bounded, reading-order blocks from one explicit representation."""
        await self._page(request); representation={'native':'native_text','layout':'layout','ocr':'ocr'}[request.representation]
        rows=await self._repo.blocks(request.organization_id,request.preprocessing_run_id,request.page_number,representation,request.limit,request.block_type)
        if request.representation=='ocr' and not rows: raise RepresentationUnavailableError
        return tuple(BlockResult(block_id=row.external_block_id,page_number=request.page_number,representation=self._rep(row.representation),extraction_source=ExtractionSource(row.extraction_source),reading_order=row.reading_order,text=row.text or '',bounding_box=self._box(row),block_type=row.block_type,font=row.font,ocr_confidence=row.ocr_confidence) for row in rows)
    async def get_page_tables(self,request:GetPageTablesInput)->tuple[TableResult,...]:
        """Return bounded physical tables in deterministic table-index order."""
        await self._page(request); rows=await self._repo.tables(request.organization_id,request.preprocessing_run_id,request.page_number,request.limit)
        return tuple(TableResult(table_id=row.external_table_id,page_number=request.page_number,table_index=row.table_index,bounding_box=self._box(row),rows=tuple(row.rows),warnings=tuple(row.warnings or [])) for row in rows)
    async def search_document(self,request:SearchDocumentInput)->tuple[SearchResult,...]:
        """Search deterministic persisted block/table text and return bounded evidence snippets."""
        await self._run(request); results=[]; reps={item.value for item in request.representations}
        if reps & {'native_text','ocr'}:
            for block,page_number in await self._repo.search_blocks(request.organization_id,request.preprocessing_run_id,request.query,request.page_number,reps & {'native_text','ocr'},request.limit):
                results.append(SearchResult(evidence=EvidenceReference(preprocessing_run_id=request.preprocessing_run_id,page_number=page_number,block_id=block.external_block_id,bounding_box=self._box(block),representation=self._rep(block.representation),extraction_source=ExtractionSource(block.extraction_source)),snippet=(block.text or '')[:1000]))
        if EvidenceRepresentation.TABLE.value in reps and len(results)<request.limit:
            for table,page_number in await self._repo.search_tables(request.organization_id,request.preprocessing_run_id,request.query,request.page_number,request.limit-len(results)):
                results.append(SearchResult(evidence=EvidenceReference(preprocessing_run_id=request.preprocessing_run_id,page_number=page_number,table_id=table.external_table_id,bounding_box=self._box(table),representation=EvidenceRepresentation.TABLE),snippet=str(table.rows)[:1000]))
        return tuple(results[:request.limit])
    async def get_page_render(self,request:GetPageRenderInput)->PageRenderResult:
        """Read and checksum-validate one bounded stored render without exposing a path."""
        page=await self._page(request)
        if not page.render_storage_key: raise RenderMissingError
        try:
            meta=await self._storage.inspect(page.render_storage_key); content=await self._storage.read(page.render_storage_key,maximum_bytes=request.maximum_bytes)
        except StorageError: raise RenderMissingError from None
        if meta.sha256_checksum!=page.render_checksum or hashlib.sha256(content).hexdigest()!=page.render_checksum: raise RenderIntegrityError
        return PageRenderResult(page_number=page.page_number,media_type=page.render_media_type,content=content,sha256_checksum=page.render_checksum,pixel_width=page.render_pixel_width,pixel_height=page.render_pixel_height,dpi=page.render_dpi)
    async def get_evidence_region(self,request:EvidenceRegionInput)->BlockResult|TableResult:
        """Resolve exactly one block or table and validate representation and optional box."""
        await self._run(request); e=request.evidence
        row=await (self._repo.table_by_id(request.organization_id,request.preprocessing_run_id,e.page_number,e.table_id) if e.table_id else self._repo.block_by_id(request.organization_id,request.preprocessing_run_id,e.page_number,e.block_id))
        if row is None: raise EvidenceNotFoundError
        actual=self._box(row)
        if e.bounding_box is not None and e.bounding_box!=actual: raise EvidenceMismatchError
        if e.table_id:
            return TableResult(table_id=row.external_table_id,page_number=e.page_number,table_index=row.table_index,bounding_box=actual,rows=tuple(row.rows),warnings=tuple(row.warnings or []))
        if row.representation!=e.representation.value or (e.extraction_source and row.extraction_source!=e.extraction_source.value): raise EvidenceMismatchError
        return BlockResult(block_id=row.external_block_id,page_number=e.page_number,representation=self._rep(row.representation),extraction_source=ExtractionSource(row.extraction_source),reading_order=row.reading_order,text=row.text or '',bounding_box=actual,block_type=row.block_type,font=row.font,ocr_confidence=row.ocr_confidence)
