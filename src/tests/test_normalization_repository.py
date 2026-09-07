"""Database-backed tests for `NormalizationRepository`.

All records are generated in the existing PostgreSQL test fixture via the
same `_seed` helper `test_sku_mapping_worker_integration.py` already uses;
no supplied PDFs, worker/API processes, or paid Claude calls anywhere in
this file. This is the only normalization test file that touches the
database -- it exists specifically to exercise the repository's real
`document_analysis_runs`/`sku_mapping_runs`/`term_applicability_runs`
lookups and completed-status gating, which the pure assembly/contract tests
cannot reach.
"""

import uuid
from datetime import UTC, datetime

import pytest

from maximor.db.session import get_session_factory
from maximor.document_analysis.agent import DocumentAnalysisRuntimeSummary
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.normalization.errors import (
    NormalizationAnalysisRunNotCompletedError,
    NormalizationTermApplicabilityRunNotCompletedError,
    NormalizationTermApplicabilityRunNotFoundError,
)
from maximor.normalization.repository import NormalizationRepository
from maximor.sku_mapping.agent import SkuMappingRuntimeSummary
from maximor.sku_mapping.contracts import SkuMappingRunArtifact, build_sku_mapping_task
from maximor.sku_mapping.persistence import SkuMappingPersistenceService
from maximor.sku_mapping.schemas import SkuMappingDecision, SkuMappingOutcome, SkuRecord, SkuRetrievalResult
from maximor.sku_mapping.versions import SKU_MAPPING_DECISION_SCHEMA_VERSION, SKU_RETRIEVAL_SCHEMA_VERSION
from maximor.storage import LocalObjectStorage
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
from maximor.term_applicability.schemas import TermApplicabilityResult
from maximor.term_triage.schemas import TermTriageResult
from test_sku_mapping_worker_integration import _seed


async def _seed_completed_sku_mapping_run(ids: dict, storage: LocalObjectStorage, organization_id: uuid.UUID) -> uuid.UUID:
    """Persist one completed MATCH SKU-mapping run for `candidate-purchased`."""

    analysis_persistence = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    result = await analysis_persistence.load_completed_result(
        organization_id=organization_id, analysis_run_id=ids["analysis_run_id"],
    )
    task = build_sku_mapping_task(
        result=result, analysis_run_id=ids["analysis_run_id"], candidate_id="candidate-purchased",
        schema_version="mapping-task-v1",
    )
    retrieval = SkuRetrievalResult(
        schema_version=SKU_RETRIEVAL_SCHEMA_VERSION, retriever_version="retriever-v1", organization_id=organization_id,
        catalog_version_id=ids["catalog_version_id"], catalog_version_identifier="v1",
        candidate_id="candidate-purchased",
    )
    decision = SkuMappingDecision(
        schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION, organization_id=organization_id, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        candidate_id="candidate-purchased", catalog_version_id=ids["catalog_version_id"],
        outcome=SkuMappingOutcome.MATCH, sku_id=ids["sku_id"], sku_code="PREMIUM_SUPPORT",
        sku_name="Premium Support", evidence=task.candidate_evidence,
    )
    artifact = SkuMappingRunArtifact(schema_version="artifact-v1", task=task, retrieval=retrieval, decision=decision)
    service = SkuMappingPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization_id, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], candidate_id="candidate-purchased", attempt_number=1,
        schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION, document_analysis_schema_version="analysis-v1", prompt_version="p",
        skill_version="s", agent_version="a", retriever_version="retriever-v1", model="fake",
        started_at=datetime.now(UTC),
    )
    authoritative_sku = SkuRecord(
        id=ids["sku_id"], source_sku_id=uuid.uuid4(), organization_id=organization_id,
        catalog_version_id=ids["catalog_version_id"], sku_code="PREMIUM_SUPPORT", name="Premium Support",
    )
    runtime = SkuMappingRuntimeSummary(
        request_id=uuid.uuid4(), started_at=datetime.now(UTC), tool_call_count=2,
        tool_calls_by_name={"retrieve_skus": 1, "get_authoritative_sku": 1},
        authoritative_skus_by_id={ids["sku_id"]: authoritative_sku},
    )
    await service.save_completed_result(run_id=run_id, artifact=artifact, runtime=runtime)
    return run_id


async def _seed_completed_term_applicability_run(ids: dict, storage: LocalObjectStorage, organization_id: uuid.UUID) -> uuid.UUID:
    """Persist one completed, empty combined term-applicability run."""

    service = TermApplicabilityPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization_id, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1, schema_version="applicability-v1",
        prompt_version="p", skill_version="s", agent_version="a", model="fake", started_at=datetime.now(UTC),
    )
    triage = TermTriageResult(
        schema_version="triage-v1", organization_id=organization_id, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"], decisions=(),
    )
    applicability = TermApplicabilityResult(
        schema_version="applicability-v1", organization_id=organization_id, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        decisions=(), candidate_commercial_facts=(), candidate_commercial_fact_coverage=(),
    )
    runtime = DocumentAnalysisRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC), tool_call_count=0)
    await service.save_completed_result(run_id=run_id, triage=triage, applicability=applicability, runtime=runtime)
    return run_id


def _repository(storage: LocalObjectStorage) -> NormalizationRepository:
    return NormalizationRepository(
        get_session_factory(),
        DocumentAnalysisPersistenceService(get_session_factory(), storage),
        TermApplicabilityPersistenceService(get_session_factory(), storage),
        SkuMappingPersistenceService(get_session_factory(), storage),
    )


@pytest.mark.asyncio
async def test_repository_loads_normalization_input_for_completed_aligned_runs(tmp_path, organization):
    """The one eligible (`purchased`) seeded candidate resolves to its completed SKU mapping."""

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    sku_run_id = await _seed_completed_sku_mapping_run(ids, storage, organization)
    term_applicability_run_id = await _seed_completed_term_applicability_run(ids, storage, organization)

    normalization_input = await _repository(storage).load_normalization_input(
        organization_id=organization, document_id=ids["document_id"], analysis_run_id=ids["analysis_run_id"],
        term_applicability_run_id=term_applicability_run_id,
    )

    assert set(normalization_input.sku_mappings) == {"candidate-purchased"}
    assert normalization_input.sku_mapping_run_ids["candidate-purchased"] == sku_run_id
    assert normalization_input.sku_mappings["candidate-purchased"].decision.outcome == SkuMappingOutcome.MATCH
    assert normalization_input.term_applicability_run_id == term_applicability_run_id


@pytest.mark.asyncio
async def test_repository_rejects_a_term_applicability_run_that_is_still_running(tmp_path, organization):
    """A `term_applicability_runs` row that never completed cannot be loaded as a source."""

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _seed_completed_sku_mapping_run(ids, storage, organization)

    service = TermApplicabilityPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1, schema_version="v", prompt_version="p",
        skill_version="s", agent_version="a", model="fake", started_at=datetime.now(UTC),
    )

    with pytest.raises(NormalizationTermApplicabilityRunNotCompletedError):
        await _repository(storage).load_normalization_input(
            organization_id=organization, document_id=ids["document_id"], analysis_run_id=ids["analysis_run_id"],
            term_applicability_run_id=run_id,
        )


@pytest.mark.asyncio
async def test_repository_rejects_a_failed_term_applicability_run(tmp_path, organization):
    """A `term_applicability_runs` row marked failed cannot be loaded as a source."""

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _seed_completed_sku_mapping_run(ids, storage, organization)

    service = TermApplicabilityPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1, schema_version="v", prompt_version="p",
        skill_version="s", agent_version="a", model="fake", started_at=datetime.now(UTC),
    )
    await service.mark_run_failed(run_id, error_code="validation_failed", error_message="safe failure")

    with pytest.raises(NormalizationTermApplicabilityRunNotCompletedError):
        await _repository(storage).load_normalization_input(
            organization_id=organization, document_id=ids["document_id"], analysis_run_id=ids["analysis_run_id"],
            term_applicability_run_id=run_id,
        )


@pytest.mark.asyncio
async def test_repository_rejects_a_term_applicability_run_naming_a_different_analysis_run(tmp_path, organization):
    """A completed term-applicability run scoped to a different analysis run is not found here.

    This is the tenant/document/analysis-run mismatch check at the
    repository boundary: the lookup query itself filters on
    `analysis_run_id`, so a cross-run id can never resolve.
    """

    storage = LocalObjectStorage(tmp_path)
    ids = await _seed(organization, storage)
    await _seed_completed_sku_mapping_run(ids, storage, organization)
    term_applicability_run_id = await _seed_completed_term_applicability_run(ids, storage, organization)

    # A second, unrelated document/analysis run under the same organization
    # (skipping the catalog seed -- an organization may have only one active
    # catalog version at a time) gives a real, completed, but *wrong*
    # analysis run to query the first term-applicability run's id against.
    other_ids = await _seed(organization, storage, with_catalog=False)

    with pytest.raises(NormalizationTermApplicabilityRunNotFoundError):
        await _repository(storage).load_normalization_input(
            organization_id=organization, document_id=other_ids["document_id"],
            analysis_run_id=other_ids["analysis_run_id"], term_applicability_run_id=term_applicability_run_id,
        )


@pytest.mark.asyncio
async def test_repository_rejects_an_analysis_run_still_running(tmp_path, organization):
    """A `document_analysis_runs` row that never completed cannot be loaded as a source."""

    storage = LocalObjectStorage(tmp_path)
    document_id, preprocessing_run_id, analysis_run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    job_id = uuid.uuid4()
    from maximor.db.models import Document, DocumentProcessingRun, ProcessingJob

    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        async with session.begin():
            session.add(Document(
                id=document_id, organization_id=organization, original_filename="generated.pdf",
                storage_key=f"generated/{document_id}.pdf", sha256_checksum=uuid.uuid4().hex + uuid.uuid4().hex,
                media_type="application/pdf", status="completed",
            ))
            session.add(ProcessingJob(
                id=job_id, organization_id=organization, document_id=document_id,
                job_type="document_preprocessing", status="completed", attempt_number=1,
            ))
            await session.flush()
            session.add(DocumentProcessingRun(
                id=preprocessing_run_id, organization_id=organization, document_id=document_id,
                processing_job_id=job_id, attempt_number=1, status="completed", schema_version="prep-v1",
                processor_version="test", original_document_checksum=uuid.uuid4().hex + uuid.uuid4().hex,
                page_count=1, started_at=now, completed_at=now,
            ))

    analysis_persistence = DocumentAnalysisPersistenceService(get_session_factory(), storage)
    await analysis_persistence.create_run(
        run_id=analysis_run_id, organization_id=organization, document_id=document_id,
        preprocessing_run_id=preprocessing_run_id, attempt_number=1, schema_version="analysis-v1",
        preprocessing_schema_version="prep-v1", prompt_version="p1", skill_version="s1", agent_version="a1",
        model="test-model", started_at=now,
    )

    with pytest.raises(NormalizationAnalysisRunNotCompletedError):
        await _repository(storage).load_normalization_input(
            organization_id=organization, document_id=document_id, analysis_run_id=analysis_run_id,
            term_applicability_run_id=uuid.uuid4(),
        )
