"""Focused tests for native text, quality, storage reads, and orchestration boundaries."""
import uuid
import pytest
from maximor.preprocessing.contracts import DocumentPreprocessingRequest, TrustedPdfSource
from maximor.preprocessing.layout_extractor import PdfplumberLayoutExtractor
from maximor.preprocessing.page_renderer import PymupdfPageRenderer
from maximor.preprocessing.pdf_inspector import PypdfPdfInspector
from maximor.preprocessing.quality import RuleBasedPageQualityEvaluator
from maximor.preprocessing.schemas import LayoutExtractionResult, NativeTextExtractionResult, TableExtractionResult
from maximor.preprocessing.service import DeterministicDocumentPreprocessor
from maximor.preprocessing.table_extractor import PdfplumberTableExtractor
from maximor.preprocessing.text_extractor import PymupdfNativeTextExtractor
from maximor.preprocessing.ocr import TesseractOcrExtractor
from maximor.storage import LocalObjectStorage, StorageError
from tests.preprocessing_pdf_factory import create_visual_pdf

async def put(storage, key, data):
    """Store one small test object."""
    async def chunks(): yield data
    return await storage.put(key, chunks(), maximum_bytes=len(data)+1)

@pytest.mark.asyncio
async def test_native_text_is_deterministic_and_preserves_literal_text(tmp_path):
    path=tmp_path/'source.pdf'; create_visual_pdf(path)
    result=await PymupdfNativeTextExtractor().extract(TrustedPdfSource(path.resolve(),'input/source.pdf'),1)
    assert result.plain_text == 'Pr0duct SKU-1'
    assert result.character_count == len(result.plain_text)
    assert result.word_count == 2
    assert [b.block_id for b in result.blocks] == ['native:p0001:b000000']

@pytest.mark.asyncio
async def test_storage_read_is_bounded_and_safe(tmp_path):
    storage=LocalObjectStorage(tmp_path); await put(storage,'objects/a.bin',b'abc')
    assert await storage.read('objects/a.bin',maximum_bytes=3) == b'abc'
    with pytest.raises(StorageError) as caught: await storage.read('objects/a.bin',maximum_bytes=2)
    assert caught.value.code == 'stored_object_too_large'

def test_quality_rules_are_deterministic():
    quality=RuleBasedPageQualityEvaluator()
    native=NativeTextExtractionResult(page_number=1,blocks=[],plain_text='',character_count=0,word_count=0)
    layout=LayoutExtractionResult(page_number=1,width_points=100,height_points=100,blocks=[])
    tables=TableExtractionResult(page_number=1,tables=[])
    result=quality.evaluate(native,layout,tables)
    assert result.ocr_required and result.readable_character_ratio == 0

@pytest.mark.asyncio
async def test_concrete_preprocessor_builds_valid_document(tmp_path):
    path=tmp_path/'source.pdf'; create_visual_pdf(path)
    storage=LocalObjectStorage(tmp_path/'objects'); data=path.read_bytes(); metadata=await put(storage,'input/source.pdf',data)
    service=DeterministicDocumentPreprocessor(storage,PypdfPdfInspector(),PymupdfNativeTextExtractor(),PdfplumberLayoutExtractor(),PdfplumberTableExtractor(),PymupdfPageRenderer(storage),RuleBasedPageQualityEvaluator(),TesseractOcrExtractor(storage))
    request=DocumentPreprocessingRequest(uuid.uuid4(),uuid.uuid4(),uuid.uuid4(),uuid.uuid4(),'input/source.pdf')
    document=await service.preprocess(request)
    assert document.original_document_checksum == metadata.sha256_checksum
    assert document.pages[0].width_points == 200
    assert document.pages[0].height_points == 300
    assert document.pages[0].ocr is None
