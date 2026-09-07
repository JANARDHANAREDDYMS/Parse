import uuid

import pytest
from sqlalchemy import select

from maximor.db.models import Document
from maximor.db.session import get_session_factory
from maximor.jobs.types import JobType


@pytest.mark.asyncio
async def test_liveness_does_not_require_database(app_client):
    _, client = app_client
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "live"}


@pytest.mark.asyncio
async def test_readiness_reports_database_state(app_client):
    _, client = app_client
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_readiness_hides_database_failure(app_client, monkeypatch):
    _, client = app_client

    async def unavailable(_engine):
        from sqlalchemy.exc import OperationalError

        raise OperationalError("safe", {}, Exception("internal"))

    monkeypatch.setattr("maximor.api.app.check_database_health", unavailable)
    response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "database_unavailable", "message": "The database is unavailable."}
    }


@pytest.mark.asyncio
async def test_valid_pdf_upload(app_client, organization, pdf_bytes):
    _, client = app_client
    response = await client.post(
        f"/v1/organizations/{organization}/documents",
        files={"file": ("order.pdf", pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["organization_id"] == str(organization)
    assert body["document_status"] == "uploaded"
    assert body["job_status"] == "queued"
    assert "storage" not in str(body).lower()

    async with get_session_factory()() as session:
        document = await session.scalar(
            select(Document).where(Document.id == uuid.UUID(body["document_id"]))
        )
    assert document is not None
    assert not document.storage_key.startswith("/")


@pytest.mark.asyncio
async def test_non_pdf_upload_is_rejected(app_client, organization):
    _, client = app_client
    response = await client.post(
        f"/v1/organizations/{organization}/documents",
        files={"file": ("note.txt", b"not pdf", "text/plain")},
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


@pytest.mark.asyncio
async def test_oversized_upload_is_rejected(app_client, organization):
    _, client = app_client
    response = await client.post(
        f"/v1/organizations/{organization}/documents",
        files={"file": ("large.pdf", b"%PDF-" + b"x" * 1024, "application/pdf")},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"


@pytest.mark.asyncio
async def test_unknown_organization(app_client, pdf_bytes):
    _, client = app_client
    response = await client.post(
        f"/v1/organizations/{uuid.uuid4()}/documents",
        files={"file": ("order.pdf", pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "organization_not_found"


@pytest.mark.asyncio
async def test_duplicate_upload_is_deliberate(app_client, organization, pdf_bytes):
    _, client = app_client
    url = f"/v1/organizations/{organization}/documents"
    first = await client.post(url, files={"file": ("one.pdf", pdf_bytes, "application/pdf")})
    second = await client.post(url, files={"file": ("two.pdf", pdf_bytes, "application/pdf")})
    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate_document"


@pytest.mark.asyncio
async def test_job_retrieval_is_tenant_scoped(app_client, organization, pdf_bytes):
    _, client = app_client
    created = await client.post(
        f"/v1/organizations/{organization}/documents",
        files={"file": ("order.pdf", pdf_bytes, "application/pdf")},
    )
    job_id = created.json()["job_id"]
    found = await client.get(f"/v1/organizations/{organization}/jobs/{job_id}")
    hidden = await client.get(f"/v1/organizations/{uuid.uuid4()}/jobs/{job_id}")
    assert found.status_code == 200
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "job_not_found"


@pytest.mark.asyncio
async def test_api_creates_preprocessing_jobs_not_smoke_jobs(
    app_client, organization, pdf_bytes
):
    app, client = app_client
    created = await client.post(
        f"/v1/organizations/{organization}/documents",
        files={"file": ("order.pdf", pdf_bytes, "application/pdf")},
    )
    status = await client.get(created.json()["job_status_url"])
    assert status.json()["job_type"] == JobType.DOCUMENT_PREPROCESSING.value
    assert all("preprocessing" not in route.path for route in app.routes)


@pytest.mark.asyncio
async def test_term_applicability_endpoint_not_found_is_safe(app_client):
    """Missing enrichment runs return a tenant-scoped safe 404."""
    _, client = app_client
    response = await client.get(f"/v1/organizations/{uuid.uuid4()}/documents/{uuid.uuid4()}/term-applicability")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "term_applicability_not_found"


@pytest.mark.asyncio
async def test_evidence_endpoint_is_tenant_scoped_and_safe_when_unavailable(app_client):
    """Evidence presentation never leaks data for a foreign or missing document."""
    _, client = app_client
    response = await client.get(
        f"/v1/organizations/{uuid.uuid4()}/documents/{uuid.uuid4()}/evidence"
    )
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "evidence_not_found", "message": "Persisted evidence is unavailable."}
    }
