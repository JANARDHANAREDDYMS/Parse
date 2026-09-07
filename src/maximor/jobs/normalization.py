"""Atomic fan-in scheduling for the normalization job."""
import uuid
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from maximor.db.models import DocumentCommercialStatusAssessment, ProcessingJob, TermApplicabilityRun
from maximor.jobs.types import JobType

# Mirrors `SkuMappingEligibilityPolicy`'s SCHEDULE disposition (purchased,
# included) -- the exact set of commercial statuses that receive a
# sku_mapping job at all. Comparing against this, not against whichever
# sku_mapping jobs already happen to exist, is what lets this function tell
# "every expected branch has finished" apart from "no branch has been
# scheduled yet" while `DocumentAnalysisHandler` is still mid-loop creating
# per-candidate sku_mapping jobs.
_ELIGIBLE_STATUSES = ("purchased", "included")


async def schedule_normalization_if_ready(session_factory, *, organization_id: uuid.UUID, document_id: uuid.UUID, analysis_run_id: uuid.UUID, completed_job_id: uuid.UUID | None = None):
    """Schedule one normalization job after every expected branch job is terminal."""
    try:
        async with session_factory() as session:
            async with session.begin():
                term_run = await session.scalar(select(TermApplicabilityRun).where(TermApplicabilityRun.organization_id == organization_id, TermApplicabilityRun.document_id == document_id, TermApplicabilityRun.analysis_run_id == analysis_run_id).order_by(TermApplicabilityRun.attempt_number.desc()))
                term_job = await session.scalar(select(ProcessingJob).where(ProcessingJob.analysis_run_id == analysis_run_id, ProcessingJob.job_type == JobType.TERM_APPLICABILITY.value).order_by(ProcessingJob.attempt_number.desc()))
                if term_run is None or term_job is None or (term_job.status in {"queued", "running"} and term_job.id != completed_job_id):
                    return None

                # Expected eligible candidates come from the persisted
                # document-analysis projections themselves, never from
                # whichever sku_mapping jobs happen to already exist --
                # otherwise a fast-finishing first candidate's job could
                # look like "every branch is done" while
                # `DocumentAnalysisHandler._schedule_eligible_sku_mapping_jobs`
                # is still mid-loop scheduling the remaining candidates.
                expected_candidate_ids = set((await session.scalars(
                    select(DocumentCommercialStatusAssessment.product_candidate_id).where(
                        DocumentCommercialStatusAssessment.analysis_run_id == analysis_run_id,
                        DocumentCommercialStatusAssessment.commercial_status.in_(_ELIGIBLE_STATUSES),
                        DocumentCommercialStatusAssessment.product_candidate_id.isnot(None),
                    )
                )).all())

                sku_jobs = list((await session.scalars(select(ProcessingJob).where(ProcessingJob.analysis_run_id == analysis_run_id, ProcessingJob.job_type == JobType.SKU_MAPPING.value))).all())
                scheduled_candidate_ids = {job.document_product_candidate_id for job in sku_jobs}
                if not expected_candidate_ids.issubset(scheduled_candidate_ids):
                    return None
                if any(job.status in {"queued", "running"} and job.id != completed_job_id for job in sku_jobs):
                    return None

                existing = await session.scalar(select(ProcessingJob).where(ProcessingJob.analysis_run_id == analysis_run_id, ProcessingJob.job_type == JobType.NORMALIZATION.value, ProcessingJob.status.in_(["queued", "running"])))
                if existing is not None:
                    return existing
                prior = list((await session.scalars(select(ProcessingJob.attempt_number).where(ProcessingJob.analysis_run_id == analysis_run_id, ProcessingJob.job_type == JobType.NORMALIZATION.value))).all())
                job = ProcessingJob(id=uuid.uuid4(), organization_id=organization_id, document_id=document_id, analysis_run_id=analysis_run_id, job_type=JobType.NORMALIZATION.value, status="queued", attempt_number=max(prior, default=0) + 1)
                session.add(job)
                await session.flush()
                return job
    except IntegrityError:
        # Another branch won the partial active-job uniqueness race.  The
        # caller need not retry or create a second job.
        async with session_factory() as session:
            return await session.scalar(select(ProcessingJob).where(ProcessingJob.analysis_run_id == analysis_run_id, ProcessingJob.job_type == JobType.NORMALIZATION.value, ProcessingJob.status.in_(["queued", "running"])))
