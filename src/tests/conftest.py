import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from maximor.api.app import create_app
from maximor.config import get_database_settings
from maximor.db.models import Organization
from maximor.db.session import get_session_factory
from maximor.storage import LocalObjectStorage


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def organization():
    organization_id = uuid.uuid4()
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            session.add(
                Organization(
                    id=organization_id,
                    slug=f"test-{organization_id.hex}",
                    name="API test organization",
                )
            )
    yield organization_id
    async with factory() as session:
        async with session.begin():
            record = await session.get(Organization, organization_id)
            if record is not None:
                await session.delete(record)


@pytest_asyncio.fixture
async def app_client(tmp_path):
    settings = get_database_settings().model_copy(
        update={"local_storage_root": tmp_path, "max_pdf_upload_bytes": 1024}
    )
    app = create_app(
        settings=settings,
        storage=LocalObjectStorage(tmp_path),
        session_factory=get_session_factory(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield app, client


@pytest.fixture
def pdf_bytes():
    return b"%PDF-1.4\nsmoke-test-only\n%%EOF\n"
