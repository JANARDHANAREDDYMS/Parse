"""Execute and persist one deterministic document-preprocessing job."""
import uuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from maximor.db.models import Document
from maximor.jobs.errors import DocumentNotFoundError, JobExecutionError
from maximor.jobs.repository import ClaimedJob, JobRepository
from maximor.jobs.types import JobContext, JobType
from maximor.preprocessing.contracts import DocumentPreprocessingRequest
from maximor.preprocessing.persistence import PreprocessingResultRepository
from maximor.preprocessing.service import DeterministicDocumentPreprocessor, PREPROCESSING_SCHEMA_VERSION, PREPROCESSOR_VERSION

class DocumentPreprocessingHandler:
    """Run preprocessing, persist it, and update document/run state but never job state."""
    def __init__(self, session_factory:async_sessionmaker[AsyncSession], preprocessor:DeterministicDocumentPreprocessor, results:PreprocessingResultRepository) -> None:
        """Receive isolated session, preprocessing, and persistence boundaries."""
        self._sessions,self._preprocessor,self._results=session_factory,preprocessor,results
    async def execute(self, context:JobContext) -> None:
        """Process one valid context and raise only safe job failures."""
        if context.job_type != JobType.DOCUMENT_PREPROCESSING.value: raise JobExecutionError('invalid_job_type','The preprocessing job type is invalid.')
        run_id=uuid.uuid5(context.processing_job_id,str(context.attempt_number))
        try:
            async with self._sessions() as s:
                doc=await JobRepository(s).get_document_for_job(ClaimedJob(context.processing_job_id,context.organization_id,context.document_id,context.job_type,context.attempt_number))
                if doc is None: raise DocumentNotFoundError
                checksum=doc.sha256_checksum
                doc.status='processing'; await s.commit()
            await self._results.start_run(run_id=run_id,organization_id=context.organization_id,document_id=context.document_id,job_id=context.processing_job_id,attempt=context.attempt_number,checksum=checksum,schema_version=PREPROCESSING_SCHEMA_VERSION,processor_version=PREPROCESSOR_VERSION,started_at=__import__('datetime').datetime.now(__import__('datetime').UTC))
            result=await self._preprocessor.preprocess(DocumentPreprocessingRequest(context.organization_id,context.document_id,context.processing_job_id,run_id,doc.storage_key))
            await self._results.save_completed_result(result)
            loaded=await self._results.load_preprocessed_document(context.organization_id,run_id)
            if loaded.preprocessing_run_id!=run_id or loaded.original_document_checksum!=checksum: raise ValueError('persisted result validation failed')
            async with self._sessions() as s:
                async with s.begin():
                    document=await s.get(Document,context.document_id)
                    if document and document.organization_id==context.organization_id: document.status='completed'
        except JobExecutionError: raise
        except Exception as exc:
            await self._results.mark_run_failed(run_id,'preprocessing_failed','Document preprocessing failed.')
            async with self._sessions() as s:
                async with s.begin():
                    document=await s.get(Document,context.document_id)
                    if document and document.organization_id==context.organization_id: document.status='failed'
            raise JobExecutionError('preprocessing_failed','Document preprocessing failed.') from exc
