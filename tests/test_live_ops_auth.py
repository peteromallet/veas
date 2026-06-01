"""Auth-hole regression tests for the live-voice ops / replay endpoints.

Closes a security review finding: three endpoints in
``app/routers/live_voice.py`` shipped with no auth —

  1. POST /api/live/sessions/{sid}/replay/{tid}  — IDOR + triggers an LLM call
  2. GET  /api/live/ops/sessions/{sid}/debug     — dumps full transcript (IDOR)
  3. GET  /api/live/ops/metrics                  — aggregate ops data leak

These tests assert the gates fire with ``LIVE_VOICE_AUTH_ENABLED=true``:

  * replay → ownership gate (403 non-owner, 401 no token, passes for owner)
  * ops/debug + ops/metrics → operator allow-list gate (403 non-operator,
    401 no token, passes for an operator in ``LIVE_VOICE_OPS_USER_IDS``)

Follows the style of tests/test_live_ownership.py (reuses its env priming and
token helpers).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers.live_voice import router as live_voice_router
from app.services.auth import jwt as live_jwt

from tests.test_live_ownership import (
    _REQUIRED_ENV,
    _SOLO_SESSION,
    _USER_A,
    _USER_B,
    _auth_header,
    _seed_pool,
    _token_for,
)
from tests.test_live_router_prep import LiveVoiceFakePool

_OPERATOR = UUID("0fff0fff-0000-0000-0000-00000000fff0")


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Clear the cached Settings before and after each test so env changes here
    don't leak into (or inherit from) other tests under random ordering."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ─────────────────────────────────────────────────────────────────────────────
# A fake pool that also answers the replay-endpoint transcript query so we can
# prove the ownership gate is the thing that rejects (or admits) the caller,
# without ever reaching a real LLM call.
# ─────────────────────────────────────────────────────────────────────────────
class OpsFakePool(LiveVoiceFakePool):
    def __init__(self) -> None:
        super().__init__()
        self._transcript_turns: dict[UUID, dict[str, Any]] = {}

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        compact = " ".join(sql.split())
        if compact.startswith("SELECT text FROM mediator.transcript_turns"):
            return self._transcript_turns.get(self._resolve_uuid(args[0]))
        # ops/metrics: spend rollup
        if "COALESCE(SUM(spend_usd_cents)" in compact:
            return {"total": 0}
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        compact = " ".join(sql.split())
        # ops/metrics: active-session count
        if "count(*) FROM mediator.conversations" in compact:
            return 0
        return await super().fetchval(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        compact = " ".join(sql.split())
        # ops/metrics: latency percentiles + status-count rollups
        if "FROM mediator.live_session_latency" in compact:
            return []
        if "count(*) AS cnt FROM mediator.conversations" in compact:
            return []
        return await super().fetch(sql, *args)


def _make_app(monkeypatch, pool: OpsFakePool, *, ops_ids: str = "") -> FastAPI:
    import app.bots.registry as _bot_reg

    monkeypatch.setenv("STAGING", "1")
    monkeypatch.setattr(_bot_reg, "_STAGING_BOTS_REGISTERED", False)
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "true")
    monkeypatch.setenv("LIVE_VOICE_OPS_USER_IDS", ops_ids)
    get_settings.cache_clear()
    _bot_reg._maybe_register_staging_bots()

    app = FastAPI()
    app.state.pool = pool
    app.include_router(live_voice_router)
    return app


def _client(monkeypatch, pool: OpsFakePool, *, ops_ids: str = "") -> TestClient:
    return TestClient(_make_app(monkeypatch, pool, ops_ids=ops_ids))


def _seeded_pool() -> OpsFakePool:
    pool = OpsFakePool()
    _seed_pool(pool)
    return pool


# ═══════════════════════════════════════════════════════════════════════════════
# 1. POST /replay/{turn_id} — ownership-gated
# ═══════════════════════════════════════════════════════════════════════════════
_TURN_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


class TestReplayOwnership:
    def test_no_token_401(self, monkeypatch):
        client = _client(monkeypatch, _seeded_pool())
        resp = client.post(
            f"/api/live/sessions/{_SOLO_SESSION}/replay/{_TURN_ID}",
        )
        assert resp.status_code == 401, resp.text

    def test_non_owner_403(self, monkeypatch):
        client = _client(monkeypatch, _seeded_pool())
        resp = client.post(
            f"/api/live/sessions/{_SOLO_SESSION}/replay/{_TURN_ID}",
            headers=_auth_header(_token_for(_USER_B)),
        )
        assert resp.status_code == 403, resp.text

    def test_unknown_session_404(self, monkeypatch):
        client = _client(monkeypatch, _seeded_pool())
        resp = client.post(
            f"/api/live/sessions/{uuid4()}/replay/{_TURN_ID}",
            headers=_auth_header(_token_for(_USER_A)),
        )
        assert resp.status_code == 404, resp.text

    def test_owner_passes_ownership_gate(self, monkeypatch):
        """Owner gets past ownership; with no matching transcript turn the
        endpoint returns 404 (NOT 403/401), proving the gate admitted them."""
        pool = _seeded_pool()
        client = _client(monkeypatch, pool)
        resp = client.post(
            f"/api/live/sessions/{_SOLO_SESSION}/replay/{_TURN_ID}",
            headers=_auth_header(_token_for(_USER_A)),
        )
        assert resp.status_code == 404, resp.text
        assert "transcript turn not found" in resp.text


# ═══════════════════════════════════════════════════════════════════════════════
# 2. GET /api/live/ops/sessions/{sid}/debug — operator-gated
# ═══════════════════════════════════════════════════════════════════════════════
class TestOpsDebugOperatorGate:
    def test_no_token_401(self, monkeypatch):
        client = _client(monkeypatch, _seeded_pool(), ops_ids=str(_OPERATOR))
        resp = client.get(f"/api/live/ops/sessions/{_SOLO_SESSION}/debug")
        assert resp.status_code == 401, resp.text

    def test_authenticated_non_operator_403(self, monkeypatch):
        client = _client(monkeypatch, _seeded_pool(), ops_ids=str(_OPERATOR))
        resp = client.get(
            f"/api/live/ops/sessions/{_SOLO_SESSION}/debug",
            headers=_auth_header(_token_for(_USER_A)),
        )
        assert resp.status_code == 403, resp.text
        assert "Operator access required" in resp.text

    def test_empty_allowlist_fails_closed(self, monkeypatch):
        """No operator configured ⇒ even an authenticated caller is denied."""
        client = _client(monkeypatch, _seeded_pool(), ops_ids="")
        resp = client.get(
            f"/api/live/ops/sessions/{_SOLO_SESSION}/debug",
            headers=_auth_header(_token_for(_OPERATOR)),
        )
        assert resp.status_code == 403, resp.text

    def test_operator_passes_gate(self, monkeypatch):
        """Operator gets past the gate; with an unknown session the endpoint
        returns 404 (NOT 403/401), proving the gate admitted them."""
        client = _client(monkeypatch, _seeded_pool(), ops_ids=str(_OPERATOR))
        resp = client.get(
            f"/api/live/ops/sessions/{uuid4()}/debug",
            headers=_auth_header(_token_for(_OPERATOR)),
        )
        assert resp.status_code == 404, resp.text


# ═══════════════════════════════════════════════════════════════════════════════
# 3. GET /api/live/ops/metrics — operator-gated
# ═══════════════════════════════════════════════════════════════════════════════
class TestOpsMetricsOperatorGate:
    def test_no_token_401(self, monkeypatch):
        client = _client(monkeypatch, _seeded_pool(), ops_ids=str(_OPERATOR))
        resp = client.get("/api/live/ops/metrics")
        assert resp.status_code == 401, resp.text

    def test_authenticated_non_operator_403(self, monkeypatch):
        client = _client(monkeypatch, _seeded_pool(), ops_ids=str(_OPERATOR))
        resp = client.get(
            "/api/live/ops/metrics",
            headers=_auth_header(_token_for(_USER_A)),
        )
        assert resp.status_code == 403, resp.text

    def test_operator_passes_gate(self, monkeypatch):
        """Operator passes the gate and reaches the (fake-pool-backed) query
        path — a non-403/401 response proves the gate admitted them."""
        client = _client(monkeypatch, _seeded_pool(), ops_ids=str(_OPERATOR))
        resp = client.get(
            "/api/live/ops/metrics",
            headers=_auth_header(_token_for(_OPERATOR)),
        )
        assert resp.status_code not in (401, 403), resp.text
