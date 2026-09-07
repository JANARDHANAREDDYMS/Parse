"""API tests for the tenant-scoped normalization retrieval endpoint.

No supplied PDFs, no paid Claude call, no worker process -- results are
seeded directly through `NormalizationPersistenceService`, mirroring how
`test_api.py` seeds other endpoints' state directly rather than driving a
full pipeline.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest

from maximor.normalization.persistence import NormalizationPersistenceService
from maximor.normalization.schemas import FinalizationIssue, NormalizationRunStatus, ReviewQueueClassification
from maximor.normalization.versions import FINALIZATION_POLICY_VERSION, NORMALIZATION_RESULT_SCHEMA_VERSION
from maximor.storage import LocalObjectStorage
from test_normalization_stage5c_persistence import _lineage, _result


async def _seed_run(storage: LocalObjectStorage, organization_id: uuid.UUID, ids: dict, *, status: NormalizationRunStatus, finalization_issues=()) -> uuid.UUID:
    service = NormalizationPersistenceService(_session_factory(), storage)
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization_id, document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        term_applicability_run_id=ids["term_applicability_run_id"], attempt_number=1, processing_job_id=None,
        schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION, finalization_policy_version=FINALIZATION_POLICY_VERSION,
        started_at=datetime.now(UTC),
    )
    result = _result(ids, organization_id, status=status)
    if finalization_issues:
        result = result.model_copy(update={"finalization_issues": finalization_issues})
    await service.save_completed_result(run_id=run_id, result=result)
    return run_id


def _session_factory():
    from maximor.db.session import get_session_factory
    return get_session_factory()


@pytest.mark.asyncio
async def test_normalization_endpoint_returns_a_completed_result(app_client, tmp_path, organization):
    _, client = app_client
    storage = LocalObjectStorage(tmp_path)
    ids = await _lineage(organization, storage)
    await _seed_run(storage, organization, ids, status=NormalizationRunStatus.COMPLETED)

    response = await client.get(f"/v1/organizations/{organization}/documents/{ids['document_id']}/normalization")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["result"] is not None
    assert body["result"]["status"] == "completed"


@pytest.mark.asyncio
async def test_normalization_endpoint_returns_a_review_required_result(app_client, tmp_path, organization):
    _, client = app_client
    storage = LocalObjectStorage(tmp_path)
    ids = await _lineage(organization, storage)
    await _seed_run(storage, organization, ids, status=NormalizationRunStatus.REVIEW_REQUIRED)

    response = await client.get(f"/v1/organizations/{organization}/documents/{ids['document_id']}/normalization")
    assert response.status_code == 200
    assert response.json()["status"] == "review_required"


@pytest.mark.asyncio
async def test_normalization_endpoint_returns_a_failed_validation_result_with_safe_issue(app_client, tmp_path, organization):
    _, client = app_client
    storage = LocalObjectStorage(tmp_path)
    ids = await _lineage(organization, storage)
    issue = FinalizationIssue(
        code="normalization_sku_mapping_missing", classification=ReviewQueueClassification.HARD_INVARIANT,
        message="An eligible candidate has no completed matching SKU mapping.", location="dependencies", hard=True,
    )
    await _seed_run(storage, organization, ids, status=NormalizationRunStatus.FAILED_VALIDATION, finalization_issues=(issue,))

    response = await client.get(f"/v1/organizations/{organization}/documents/{ids['document_id']}/normalization")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed_validation"
    assert body["result"]["finalization_issues"][0]["code"] == "normalization_sku_mapping_missing"


@pytest.mark.asyncio
async def test_normalization_endpoint_is_safely_not_found_when_no_run_exists(app_client):
    _, client = app_client
    response = await client.get(f"/v1/organizations/{uuid.uuid4()}/documents/{uuid.uuid4()}/normalization")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "normalization_not_found", "message": "Normalization is unavailable."}}


@pytest.mark.asyncio
async def test_normalization_endpoint_is_tenant_scoped(app_client, tmp_path, organization):
    _, client = app_client
    storage = LocalObjectStorage(tmp_path)
    ids = await _lineage(organization, storage)
    await _seed_run(storage, organization, ids, status=NormalizationRunStatus.COMPLETED)

    response = await client.get(f"/v1/organizations/{uuid.uuid4()}/documents/{ids['document_id']}/normalization")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_normalization_endpoint_response_contains_no_storage_path_sql_or_prompt_text(app_client, tmp_path, organization):
    _, client = app_client
    storage = LocalObjectStorage(tmp_path)
    ids = await _lineage(organization, storage)
    await _seed_run(storage, organization, ids, status=NormalizationRunStatus.COMPLETED)

    response = await client.get(f"/v1/organizations/{organization}/documents/{ids['document_id']}/normalization")
    text = json.dumps(response.json())
    for forbidden in (".pdf", "storage_key", str(tmp_path), "SELECT ", "INSERT INTO", "prompt", "sk-ant-"):
        assert forbidden not in text
