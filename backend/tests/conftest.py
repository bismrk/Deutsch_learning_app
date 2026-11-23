from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Provide an ASGI AsyncClient backed by an isolated test FastAPI app."""
    test_app = create_app(settings=Settings(_env_file=None, app_env="test"))
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client
