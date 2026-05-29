"""Tests for live-voice identity primitives (Sprint 1 T2).

Covers:
(a) auth disabled → get_current_user returns the configured test UUID.
(b) auth enabled + valid minted token → returns that UUID.
(c) auth enabled + missing header → 401.
(d) auth enabled + garbage token → 401.
(e) _require_ownership: owner match, partner match, NULL-partner non-owner (no
    crash, 403), unknown session (404).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers.live_voice import _require_ownership, get_current_user
from app.services.auth import jwt as live_jwt

# --------------------------------------------------------------------------- #
# Shared env-priming helper
# --------------------------------------------------------------------------- #

_REQUIRED_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "dummy-service-role",
    "ANTHROPIC_API_KEY": "dummy-anthropic",
    "OPENAI_API_KEY": "dummy-openai",
    "GROQ_API_KEY": "dummy-groq",
    "WHATSAPP_TOKEN": "dummy-whatsapp",
    "WHATSAPP_VERIFY_TOKEN": "dummy-verify",
    "ADMIN_PASSWORD": "dummy-admin",
}


def _prime(monkeypatch) -> None:
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Tiny FastAPI app that exposes get_current_user as a dependency
# --------------------------------------------------------------------------- #

def _make_whoami_app() -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    def whoami(user_id: UUID = Depends(get_current_user)) -> dict[str, str]:
        return {"user_id": str(user_id)}

    return app


# --------------------------------------------------------------------------- #
# Tests: get_current_user via TestClient
# --------------------------------------------------------------------------- #

class TestGetCurrentUser:
    def test_auth_disabled_returns_test_user(self, monkeypatch):
        _prime(monkeypatch)
        monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "false")
        monkeypatch.setenv("LIVE_VOICE_TEST_USER_ID", "00000000-0000-0000-0000-000000000099")
        get_settings.cache_clear()

        client = TestClient(_make_whoami_app())
        resp = client.get("/whoami")
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "00000000-0000-0000-0000-000000000099"

    def test_auth_enabled_valid_token_returns_uuid(self, monkeypatch):
        _prime(monkeypatch)
        monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "true")
        get_settings.cache_clear()

        expected_uid = str(uuid4())
        token = live_jwt.mint(user_id=expected_uid)

        client = TestClient(_make_whoami_app())
        resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == expected_uid

    def test_auth_enabled_missing_header_returns_401(self, monkeypatch):
        _prime(monkeypatch)
        monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "true")
        get_settings.cache_clear()

        client = TestClient(_make_whoami_app(), raise_server_exceptions=False)
        resp = client.get("/whoami")
        assert resp.status_code == 401

    def test_auth_enabled_garbage_token_returns_401(self, monkeypatch):
        _prime(monkeypatch)
        monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "true")
        get_settings.cache_clear()

        client = TestClient(_make_whoami_app(), raise_server_exceptions=False)
        resp = client.get("/whoami", headers={"Authorization": "Bearer garbage.token.here"})
        assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Minimal fake pool for _require_ownership tests
# --------------------------------------------------------------------------- #

class _OwnershipFakePool:
    def __init__(self, rows: dict[UUID, dict[str, Any]]) -> None:
        self._rows = rows

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return self._rows.get(args[0])


# --------------------------------------------------------------------------- #
# Tests: _require_ownership
# --------------------------------------------------------------------------- #

class TestRequireOwnership:
    async def test_owner_match_returns_row(self):
        owner_id = uuid4()
        session_id = uuid4()
        pool = _OwnershipFakePool({
            session_id: {
                "id": session_id,
                "user_id": owner_id,
                "partner_user_id": None,
                "status": "active",
            }
        })
        row = await _require_ownership(pool, session_id, owner_id)
        assert row["user_id"] == owner_id

    async def test_partner_match_returns_row(self):
        owner_id = uuid4()
        partner_id = uuid4()
        session_id = uuid4()
        pool = _OwnershipFakePool({
            session_id: {
                "id": session_id,
                "user_id": owner_id,
                "partner_user_id": partner_id,
                "status": "active",
            }
        })
        row = await _require_ownership(pool, session_id, partner_id)
        assert row["partner_user_id"] == partner_id

    async def test_null_partner_non_owner_raises_403_no_crash(self):
        from fastapi import HTTPException
        owner_id = uuid4()
        other_id = uuid4()
        session_id = uuid4()
        pool = _OwnershipFakePool({
            session_id: {
                "id": session_id,
                "user_id": owner_id,
                "partner_user_id": None,
                "status": "active",
            }
        })
        with pytest.raises(HTTPException) as exc_info:
            await _require_ownership(pool, session_id, other_id)
        assert exc_info.value.status_code == 403

    async def test_unknown_session_raises_404(self):
        from fastapi import HTTPException
        pool = _OwnershipFakePool({})
        with pytest.raises(HTTPException) as exc_info:
            await _require_ownership(pool, uuid4(), uuid4())
        assert exc_info.value.status_code == 404
