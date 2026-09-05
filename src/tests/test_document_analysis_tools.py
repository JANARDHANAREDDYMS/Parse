"""Test narrow document-tool bounds and integrity behavior with generated in-memory fakes."""
import hashlib
import uuid
import pytest
from pydantic import ValidationError
from maximor.document_analysis.errors import ProcessingRunNotCompletedError, RenderIntegrityError
from maximor.document_analysis.tool_schemas import GetPageRenderInput, SearchDocumentInput, ToolScope
from maximor.document_analysis.tools import PersistedDocumentTools
from maximor.document_analysis.repository import DocumentAnalysisRepository
from maximor.db.models import Document, DocumentBlock, DocumentPage, DocumentProcessingRun, ProcessingJob
from maximor.db.session import get_session_factory
from maximor.storage import LocalObjectStorage

class Run:
    """Minimal generated completed run record."""
    status='completed'; document_id=uuid.uuid4(); schema_version='1'; processor_version='1'; page_count=1; warnings=[]

class Page:
    """Minimal generated page/render record without filesystem paths."""
    page_number=1; render_storage_key='renders/page.png'; render_checksum=hashlib.sha256(b'valid').hexdigest(); render_media_type='image/png'; render_pixel_width=10; render_pixel_height=10; render_dpi=72

class Repository:
    """In-memory repository double that exposes only narrow repository operations."""
    async def run(self,*_): return Run()
    async def page(self,*_): return Page()

class Storage:
    """In-memory object-storage double used to test checksum enforcement."""
    async def inspect(self,*_):
        return type('Meta',(),{'sha256_checksum':hashlib.sha256(b'valid').hexdigest(),'size':5})()
    async def read(self,*_,**__): return b'valid'

@pytest.mark.asyncio
async def test_render_returns_validated_bytes_without_path():
    """Return only typed render metadata/bytes after checksum validation."""
    tools=PersistedDocumentTools(Repository(),Storage()); scope=GetPageRenderInput(organization_id=uuid.uuid4(),preprocessing_run_id=uuid.uuid4(),page_number=1)
    result=await tools.get_page_render(scope)
    assert result.content==b'valid' and 'path' not in result.model_dump()

@pytest.mark.asyncio
async def test_render_checksum_mismatch_is_safe():
    """Reject corrupt stored bytes with a safe typed error."""
    class BadStorage(Storage):
        async def read(self,*_,**__): return b'corrupt'
    tools=PersistedDocumentTools(Repository(),BadStorage())
    with pytest.raises(RenderIntegrityError):
        await tools.get_page_render(GetPageRenderInput(organization_id=uuid.uuid4(),preprocessing_run_id=uuid.uuid4(),page_number=1))

def test_search_limits_and_scope_are_bounded():
    """Reject excessive limits and retain only tenant/run identifiers in scope."""
    with pytest.raises(ValidationError):
        SearchDocumentInput(organization_id=uuid.uuid4(),preprocessing_run_id=uuid.uuid4(),query='x',limit=101)
    assert set(ToolScope.model_fields)=={'organization_id','preprocessing_run_id'}


@pytest.mark.asyncio
async def test_database_tools_are_tenant_scoped_and_keep_text_representations_separate(tmp_path, organization):
    """Query generated persisted records through the real tenant-scoped repository."""
    document_id, job_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    factory=get_session_factory()
    async with factory() as session:
        async with session.begin():
            session.add(Document(id=document_id,organization_id=organization,original_filename='generated.pdf',storage_key=f'generated/{document_id}.pdf',sha256_checksum='a'*64,media_type='application/pdf',status='completed'))
            session.add(ProcessingJob(id=job_id,organization_id=organization,document_id=document_id,job_type='document_preprocessing',status='completed',attempt_number=1))
            await session.flush()
            session.add(DocumentProcessingRun(id=run_id,organization_id=organization,document_id=document_id,processing_job_id=job_id,attempt_number=1,status='completed',schema_version='1',processor_version='1',original_document_checksum='a'*64,page_count=1,started_at=__import__('datetime').datetime.now(__import__('datetime').UTC)))
            await session.flush()
            page=DocumentPage(organization_id=organization,processing_run_id=run_id,page_number=1,width_points=100,height_points=100,rotation_degrees=0,native_plain_text='native only',native_character_count=11,native_word_count=2,ocr_status='completed',ocr_plain_text='ocr only',quality={},render_storage_key='generated/render.png',render_media_type='image/png',render_pixel_width=1,render_pixel_height=1,render_dpi=72,render_checksum='b'*64,warnings=[],created_at=__import__('datetime').datetime.now(__import__('datetime').UTC)); session.add(page); await session.flush()
            session.add_all([DocumentBlock(organization_id=organization,processing_run_id=run_id,document_page_id=page.id,external_block_id='native:p0001:b000000',representation='native_text',extraction_source='native',reading_order=0,text='native only',x0=0,y0=0,x1=10,y1=10,created_at=__import__('datetime').datetime.now(__import__('datetime').UTC)),DocumentBlock(organization_id=organization,processing_run_id=run_id,document_page_id=page.id,external_block_id='ocr:p0001:b000000',representation='ocr',extraction_source='ocr',reading_order=0,text='ocr only',x0=0,y0=0,x1=10,y1=10,created_at=__import__('datetime').datetime.now(__import__('datetime').UTC))])
    tools=PersistedDocumentTools(DocumentAnalysisRepository(factory),LocalObjectStorage(tmp_path))
    native=await tools.get_page_text(__import__('maximor.document_analysis.tool_schemas',fromlist=['GetPageTextInput']).GetPageTextInput(organization_id=organization,preprocessing_run_id=run_id,page_number=1,representation='native'))
    ocr=await tools.get_page_text(__import__('maximor.document_analysis.tool_schemas',fromlist=['GetPageTextInput']).GetPageTextInput(organization_id=organization,preprocessing_run_id=run_id,page_number=1,representation='ocr'))
    assert native[0].text=='native only' and ocr[0].text=='ocr only'
    with pytest.raises(Exception):
        await tools.get_page_text(__import__('maximor.document_analysis.tool_schemas',fromlist=['GetPageTextInput']).GetPageTextInput(organization_id=uuid.uuid4(),preprocessing_run_id=run_id,page_number=1,representation='native'))
