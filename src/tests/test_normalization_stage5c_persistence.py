"""Database-backed tests for `NormalizationPersistenceService`.

No supplied PDFs, worker/API processes, or paid Claude calls. Real lineage
rows (organization, document, preprocessing/analysis/sku-mapping/
term-applicability runs) come from the existing `_seed`/Stage 5C repository
seed helpers already used across this codebase's other test files.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest

from maximor.db.models import NormalizationRun
from maximor.db.session import get_session_factory
from maximor.normalization.persistence import NormalizationPersistenceError, NormalizationPersistenceService
from maximor.normalization.schemas import FinalOrderFormExtraction, NormalizationResult, NormalizationRunStatus, ValidationStatus
from maximor.normalization.versions import FINALIZATION_POLICY_VERSION, NORMALIZATION_RESULT_SCHEMA_VERSION
from maximor.storage import LocalObjectStorage
from test_normalization_repository import _seed_completed_sku_mapping_run, _seed_completed_term_applicability_run
from test_sku_mapping_worker_integration import _seed


async def _lineage(organization_id: uuid.UUID, storage: LocalObjectStorage) -> dict:
    """Seed one full completed analysis + sku-mapping + term-applicability chain."""

    ids = await _seed(organization_id, storage)
    await _seed_completed_sku_mapping_run(ids, storage, organization_id)
    term_applicability_run_id = await _seed_completed_term_applicability_run(ids, storage, organization_id)
    ids["term_applicability_run_id"] = term_applicability_run_id
    return ids


def _result(ids: dict, organization_id: uuid.UUID, *, status: NormalizationRunStatus) -> NormalizationResult:
    extraction = FinalOrderFormExtraction(
        schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION, organization_id=organization_id,
        document_id=ids["document_id"], preprocessing_run_id=ids["preprocessing_run_id"],
        analysis_run_id=ids["analysis_run_id"], term_applicability_run_id=ids["term_applicability_run_id"],
        validation_status=ValidationStatus.COMPLETED if status is NormalizationRunStatus.COMPLETED else ValidationStatus.REVIEW_REQUIRED,
    )
    return NormalizationResult(
        schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION, organization_id=organization_id,
        document_id=ids["document_id"], preprocessing_run_id=ids["preprocessing_run_id"],
        analysis_run_id=ids["analysis_run_id"], term_applicability_run_id=ids["term_applicability_run_id"],
        finalization_policy_version=FINALIZATION_POLICY_VERSION, status=status, extraction=extraction,
    )


@pytest.mark.asyncio
async def test_completed_result_round_trips_with_matching_checksum(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _lineage(organization, storage)
    service = NormalizationPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        term_applicability_run_id=ids["term_applicability_run_id"], attempt_number=1, processing_job_id=None,
        schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION, finalization_policy_version=FINALIZATION_POLICY_VERSION,
        started_at=datetime.now(UTC),
    )
    result = _result(ids, organization, status=NormalizationRunStatus.COMPLETED)
    key, checksum = await service.save_completed_result(run_id=run_id, result=result)
    assert key.endswith("normalized-result.json.gz")
    assert checksum

    loaded = await service.load_completed_result(organization_id=organization, run_id=run_id)
    assert loaded == result

    async with get_session_factory()() as session:
        row = await session.get(NormalizationRun, run_id)
    assert row.status == "completed"
    assert row.canonical_result_storage_key == key
    assert row.compressed_sha256_checksum == checksum


@pytest.mark.asyncio
async def test_review_required_result_persists_and_reloads(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _lineage(organization, storage)
    service = NormalizationPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        term_applicability_run_id=ids["term_applicability_run_id"], attempt_number=1, processing_job_id=None,
        schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION, finalization_policy_version=FINALIZATION_POLICY_VERSION,
        started_at=datetime.now(UTC),
    )
    result = _result(ids, organization, status=NormalizationRunStatus.REVIEW_REQUIRED)
    await service.save_completed_result(run_id=run_id, result=result)
    loaded = await service.load_completed_result(organization_id=organization, run_id=run_id)
    assert loaded.status is NormalizationRunStatus.REVIEW_REQUIRED
    async with get_session_factory()() as session:
        row = await session.get(NormalizationRun, run_id)
    assert row.status == "review_required"


@pytest.mark.asyncio
async def test_tenant_isolation_rejects_a_cross_organization_load(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _lineage(organization, storage)
    service = NormalizationPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        term_applicability_run_id=ids["term_applicability_run_id"], attempt_number=1, processing_job_id=None,
        schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION, finalization_policy_version=FINALIZATION_POLICY_VERSION,
        started_at=datetime.now(UTC),
    )
    await service.save_completed_result(run_id=run_id, result=_result(ids, organization, status=NormalizationRunStatus.COMPLETED))

    with pytest.raises(NormalizationPersistenceError):
        await service.load_completed_result(organization_id=uuid.uuid4(), run_id=run_id)


@pytest.mark.asyncio
async def test_resubmitting_the_identical_completed_result_is_idempotent(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _lineage(organization, storage)
    service = NormalizationPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        term_applicability_run_id=ids["term_applicability_run_id"], attempt_number=1, processing_job_id=None,
        schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION, finalization_policy_version=FINALIZATION_POLICY_VERSION,
        started_at=datetime.now(UTC),
    )
    result = _result(ids, organization, status=NormalizationRunStatus.COMPLETED)
    first_key, first_checksum = await service.save_completed_result(run_id=run_id, result=result)
    second_key, second_checksum = await service.save_completed_result(run_id=run_id, result=result)
    assert first_key == second_key
    assert first_checksum == second_checksum


@pytest.mark.asyncio
async def test_resubmitting_a_conflicting_result_is_rejected(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _lineage(organization, storage)
    service = NormalizationPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        term_applicability_run_id=ids["term_applicability_run_id"], attempt_number=1, processing_job_id=None,
        schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION, finalization_policy_version=FINALIZATION_POLICY_VERSION,
        started_at=datetime.now(UTC),
    )
    await service.save_completed_result(run_id=run_id, result=_result(ids, organization, status=NormalizationRunStatus.COMPLETED))
    with pytest.raises(NormalizationPersistenceError):
        await service.save_completed_result(run_id=run_id, result=_result(ids, organization, status=NormalizationRunStatus.REVIEW_REQUIRED))


@pytest.mark.asyncio
async def test_mark_run_failed_never_overwrites_an_accepted_terminal_result(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _lineage(organization, storage)
    service = NormalizationPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        term_applicability_run_id=ids["term_applicability_run_id"], attempt_number=1, processing_job_id=None,
        schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION, finalization_policy_version=FINALIZATION_POLICY_VERSION,
        started_at=datetime.now(UTC),
    )
    await service.save_completed_result(run_id=run_id, result=_result(ids, organization, status=NormalizationRunStatus.COMPLETED))
    with pytest.raises(NormalizationPersistenceError):
        await service.mark_run_failed(run_id, error_code="normalization_failed")


@pytest.mark.asyncio
async def test_persisted_artifact_contains_no_storage_path_sql_or_prompt_text(tmp_path, organization):
    storage = LocalObjectStorage(tmp_path)
    ids = await _lineage(organization, storage)
    service = NormalizationPersistenceService(get_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        term_applicability_run_id=ids["term_applicability_run_id"], attempt_number=1, processing_job_id=None,
        schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION, finalization_policy_version=FINALIZATION_POLICY_VERSION,
        started_at=datetime.now(UTC),
    )
    result = _result(ids, organization, status=NormalizationRunStatus.COMPLETED)
    await service.save_completed_result(run_id=run_id, result=result)
    loaded = await service.load_completed_result(organization_id=organization, run_id=run_id)
    text = json.dumps(loaded.model_dump(mode="json"))
    for forbidden in (".pdf", "storage_key", str(tmp_path), "SELECT ", "INSERT INTO", "prompt"):
        assert forbidden not in text
