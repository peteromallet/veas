import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.health import router
from app.config import get_settings


class SuccessConnection:
    async def execute(self, sql: str) -> str:
        assert sql == "SELECT 1"
        return "SELECT 1"


class AcquireContext:
    async def __aenter__(self) -> SuccessConnection:
        return SuccessConnection()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class SuccessPool:
    def acquire(self) -> AcquireContext:
        return AcquireContext()


class FailingPool:
    def acquire(self):
        raise RuntimeError("database unavailable")


def test_health_returns_ok_after_ping() -> None:
    app = FastAPI()
    app.state.pool = SuccessPool()
    app.include_router(router)
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


def test_health_returns_503_on_db_error() -> None:
    app = FastAPI()
    app.state.pool = FailingPool()
    app.include_router(router)
    response = TestClient(app).get("/health")
    assert response.status_code == 503
    assert "database unavailable" in response.json()["error"]


def _deep_app(monkeypatch, pool):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "dummy-service-role")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai")
    monkeypatch.setenv("GROQ_API_KEY", "dummy-groq")
    monkeypatch.setenv("WHATSAPP_TOKEN", "dummy-whatsapp")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "12345")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "dummy-verify")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "dummy-secret")
    monkeypatch.setenv("ADMIN_PASSWORD", "dummy-admin")
    monkeypatch.setenv("PARTNER_PHONE_A", "15555550100")
    monkeypatch.setenv("PARTNER_PHONE_B", "15555550101")
    get_settings.cache_clear()
    import app.routers.health as health_module
    health_module._anthropic_cache.update(checked_at=None, ok=None, error=None)
    app = FastAPI()
    app.state.pool = pool
    app.include_router(router)
    return app


def test_deep_health_success_and_anthropic_cache(monkeypatch) -> None:
    calls = []

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers):
            calls.append((url, headers))
            return Response()

    monkeypatch.setattr("app.routers.health.httpx.AsyncClient", Client)
    client = TestClient(_deep_app(monkeypatch, SuccessPool()))

    assert client.get("/health/deep").status_code == 200
    second = client.get("/health/deep")
    assert second.status_code == 200
    assert second.json()["anthropic"]["cached"] is True
    assert len(calls) == 1
    get_settings.cache_clear()


def test_deep_health_503_on_required_check_failure(monkeypatch) -> None:
    class Client:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers):
            raise RuntimeError("anthropic unavailable")

    monkeypatch.setattr("app.routers.health.httpx.AsyncClient", Client)
    response = TestClient(_deep_app(monkeypatch, SuccessPool())).get("/health/deep")

    assert response.status_code == 503
    assert response.json()["anthropic"]["status"] == "error"
    get_settings.cache_clear()


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL unset")
def test_health_returns_ok_with_test_database() -> None:
    asyncpg = pytest.importorskip("asyncpg")

    async def create_pool():
        return await asyncpg.create_pool(os.environ["TEST_DATABASE_URL"])

    import asyncio

    pool = asyncio.run(create_pool())
    app = FastAPI()
    app.state.pool = pool
    app.include_router(router)
    try:
        response = TestClient(app).get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "db": "ok"}
    finally:
        asyncio.run(pool.close())
