"""Build the background worker with only implemented job handlers registered."""

from maximor.config import get_database_settings
from maximor.db.session import get_session_factory
from maximor.jobs.dispatcher import JobDispatcher
from maximor.jobs.types import JobType
from maximor.storage import LocalObjectStorage
from maximor.worker.handlers import PipelineSmokeTestHandler, DocumentPreprocessingHandler, DocumentAnalysisHandler, SkuMappingHandler, TermApplicabilityHandler, NormalizationHandler
from maximor.preprocessing.pdf_inspector import PypdfPdfInspector
from maximor.preprocessing.text_extractor import PymupdfNativeTextExtractor
from maximor.preprocessing.layout_extractor import PdfplumberLayoutExtractor
from maximor.preprocessing.table_extractor import PdfplumberTableExtractor
from maximor.preprocessing.page_renderer import PymupdfPageRenderer
from maximor.preprocessing.quality import RuleBasedPageQualityEvaluator
from maximor.preprocessing.ocr import TesseractOcrExtractor
from maximor.preprocessing.service import DeterministicDocumentPreprocessor
from maximor.preprocessing.persistence import PreprocessingResultRepository
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.sku_mapping.persistence import SkuMappingPersistenceService
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
from maximor.normalization.persistence import NormalizationPersistenceService
from maximor.worker.runner import WorkerRunner


def build_worker() -> WorkerRunner:
    """Construct a runner with every implemented job handler registered."""

    settings = get_database_settings()
    session_factory = get_session_factory()
    storage = LocalObjectStorage(settings.local_storage_root)
    preprocessor = DeterministicDocumentPreprocessor(storage, PypdfPdfInspector(), PymupdfNativeTextExtractor(), PdfplumberLayoutExtractor(), PdfplumberTableExtractor(), PymupdfPageRenderer(storage), RuleBasedPageQualityEvaluator(), TesseractOcrExtractor(storage))
    results = PreprocessingResultRepository(session_factory, storage)
    analysis_results = DocumentAnalysisPersistenceService(session_factory, storage)
    mapping_results = SkuMappingPersistenceService(session_factory, storage)
    enrichment_results = TermApplicabilityPersistenceService(session_factory, storage)
    normalization_results = NormalizationPersistenceService(session_factory, storage)
    dispatcher = JobDispatcher(
        {
            JobType.PIPELINE_SMOKE_TEST: PipelineSmokeTestHandler(
                session_factory, storage
            ),
            JobType.DOCUMENT_PREPROCESSING: DocumentPreprocessingHandler(session_factory, preprocessor, results),
            JobType.DOCUMENT_ANALYSIS: DocumentAnalysisHandler(settings, session_factory, storage, analysis_results),
            JobType.SKU_MAPPING: SkuMappingHandler(settings, session_factory, storage, analysis_results, mapping_results),
            JobType.TERM_APPLICABILITY: TermApplicabilityHandler(settings, session_factory, storage, analysis_results, enrichment_results),
            JobType.NORMALIZATION: NormalizationHandler(settings, session_factory, analysis_results, enrichment_results, mapping_results, normalization_results),
        }
    )
    return WorkerRunner(
        settings=settings,
        session_factory=session_factory,
        dispatcher=dispatcher,
    )


async def run_worker(*, once: bool = False) -> int:
    """Run the configured background worker continuously or for at most one job."""

    return await build_worker().run(once=once)


__all__ = ["WorkerRunner", "build_worker", "run_worker"]
