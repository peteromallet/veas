"""Tests for live-voice WebSocket ownership enforcement (Sprint 1 T9).

Covers:
(a) Non-owner WS connection → close code 4003 with no preceding phase frames.
(b) Owner WS connection → normal phase/ready frames and status flip to 'active'.
(c) create→discover→start round trip as user A (HTTP create + sessions list +
    WS connect).
(d) Dev fallback: with live_voice_auth_enabled=False, unauthenticated calls
    resolve to the test user, ownership skipped on both HTTP and WS, existing
    flow works.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.bots.registry import _maybe_register_staging_bots
from app.config import get_settings
from app.routers.live_voice import router as live_voice_router
from app.services.auth import jwt as live_jwt

# ── reuse FakePool from prep tests ───────────────────────────────────────────
from tests.test_live_router_prep import LiveVoiceFakePool, _fake_primary_topic_id_for

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

_REQUIRED_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql://user:***@localhost:5432/db",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "dummy-service-role",
    "ANTHROPIC_API_KEY": "dummy-anthropic",
    "OPENAI_API_KEY": "dummy-openai",
    "GROQ_API_KEY": "dummy-groq",
    "WHATSAPP_TOKEN": "dummy-whatsapp",
    "WHATSAPP_BEARER_TOKEN": "dummy-whatsapp",
    "WHATSAPP_PHONE_NUMBER_ID": "12345",
    "WHATSAPP_VERIFY_TOKEN": "dummy-verify",
    "WHATSAPP_APP_SECRET": "dummy-secret",
    "ADMIN_PASSWORD": "dummy-admin",
    "PARTNER_PHONE_A": "15555550100",
    "PARTNER_PHONE_B": "15555550101",
    "DISCORD_PARTNER_USER_ID_A": "",
    "DISCORD_PARTNER_USER_ID_B": "",
    "SUPABASE_STORAGE_BUCKET": "mediator-media",
    "MEDIA_FETCH_TIMEOUT_S": "30",
    "DEFAULT_USER_TIMEZONE": "UTC",
}

_USER_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_USER_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_USER_C = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_SESSION_A = UUID("11111111-1111-1111-1111-111111111111")
_SESSION_B = UUID("22222222-2222-2222-2222-222222222222")
_SESSION_C = UUID("33333333-3333-3333-3333-333333333333")  # A's solo session


def _prime(monkeypatch, *, auth_enabled: bool = True) -> None:
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "true" if auth_enabled else "false")
    monkeypatch.setenv("STAGING", "1")
    get_settings.cache_clear()


def _token_for(user_id: UUID) -> str:
    return live_jwt.mint(user_id=str(user_id))


def _make_app(monkeypatch, pool: LiveVoiceFakePool, *, auth_enabled: bool = True) -> FastAPI:
    """Build a FastAPI test app with the live_voice router and our fake pool."""
    import app.bots.registry as _bot_reg

    monkeypatch.setenv("STAGING", "1")
    monkeypatch.setattr(_bot_reg, "_STAGING_BOTS_REGISTERED", False)
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "true" if auth_enabled else "false")
    get_settings.cache_clear()

    _bot_reg._maybe_register_staging_bots()

    app = FastAPI()
    app.state.pool = pool
    app.include_router(live_voice_router)
    return app


def _client(monkeypatch, pool: LiveVoiceFakePool, *, auth_enabled: bool = True) -> TestClient:
    return TestClient(_make_app(monkeypatch, pool, auth_enabled=auth_enabled))


def _seed_ws_pool(pool: LiveVoiceFakePool) -> None:
    """Seed conversations for WS ownership tests.

    * _SESSION_A: user_id=A, partner=B,   status='ready'  (shared)
    * _SESSION_B: user_id=B, partner=None, status='ready'  (B's solo)
    * _SESSION_C: user_id=A, partner=None, status='ready'  (A's solo)
    """
    pool._conversations[_SESSION_A] = {
        "id": _SESSION_A,
        "user_id": _USER_A,
        "partner_user_id": _USER_B,
        "bot_id": "tante_rosi",
        "mode": "open",
        "status": "ready",
        "prep_summary": "Ready session A.",
        "steering_text": None,
        "current_item_id": None,
        "started_at": None,
        "session_fields": {},
        "created_at": "2026-05-01T10:00:00Z",
    }
    pool._conversations[_SESSION_B] = {
        "id": _SESSION_B,
        "user_id": _USER_B,
        "partner_user_id": None,
        "bot_id": "tante_rosi",
        "mode": "open",
        "status": "ready",
        "prep_summary": "Ready session B.",
        "steering_text": None,
        "current_item_id": None,
        "started_at": None,
        "session_fields": {},
        "created_at": "2026-05-02T10:00:00Z",
    }
    pool._conversations[_SESSION_C] = {
        "id": _SESSION_C,
        "user_id": _USER_A,
        "partner_user_id": None,
        "bot_id": "tante_rosi",
        "mode": "open",
        "status": "ready",
        "prep_summary": "Ready session C — solo A.",
        "steering_text": None,
        "current_item_id": None,
        "started_at": None,
        "session_fields": {},
        "created_at": "2026-05-03T10:00:00Z",
    }


def _ws_url(session_id: UUID, token: str | None = None) -> str:
    """Build WS URL with optional token query param."""
    base = f"/ws/live/{session_id}"
    if token:
        base += f"?token={token}"
    return base


def _drain_ws(
    ws,
    *,
    max_frames: int = 10,
) -> tuple[list[dict[str, Any]], int | None]:
    """Receive up to max_frames from WS, catching WebSocketDisconnect.

    Returns (frames, close_code). close_code is None if the WS is still open
    after receiving max_frames.  Stops early when a 'ready' frame is received
    so tests don't hang waiting for forward_events.
    """
    frames: list[dict[str, Any]] = []
    close_code: int | None = None
    for _ in range(max_frames):
        try:
            data = ws.receive_json()
            frames.append(data)
            # Stop collecting after 'ready' frame — the WS will keep running
            # with forward_events but the test is done.
            if data.get("type") == "ready":
                break
        except WebSocketDisconnect as exc:
            close_code = exc.code
            break
        except Exception:
            break
    return frames, close_code


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWSOwnership:
    """WS ownership enforcement with auth enabled."""

    def test_non_owner_receives_4003_no_phase_frames(self, monkeypatch):
        """B's token against A's solo session → close code 4003, no phase frames."""
        pool = LiveVoiceFakePool()
        _seed_ws_pool(pool)
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch, auth_enabled=True)
        monkeypatch.setattr(
            "app.routers.live_voice.select_transcriber",
            lambda: _NoopTranscriber(),
        )
        client = _client(monkeypatch, pool, auth_enabled=True)

        token_b = _token_for(_USER_B)
        # B tries to access A's solo session (SESSION_C) — B is not an owner.
        url = _ws_url(_SESSION_C, token_b)

        with client.websocket_connect(url) as ws:
            frames, close_code = _drain_ws(ws)

        # Should have zero phase/ready frames.
        phase_or_ready = [
            f for f in frames
            if f.get("type") in ("phase", "ready")
        ]
        assert len(phase_or_ready) == 0, (
            f"expected no phase/ready frames before 4003 close, got {phase_or_ready}"
        )
        assert close_code == 4003, (
            f"expected close_code=4003, got {close_code}"
        )

    def test_owner_receives_phase_frames_and_active_status(self, monkeypatch):
        """A's token against A's session → normal phase/ready frames, status→'active'."""
        pool = LiveVoiceFakePool()
        _seed_ws_pool(pool)
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch, auth_enabled=True)
        # Disable real transcriber / turn caller to prevent background loops.
        monkeypatch.setattr(
            "app.routers.live_voice.select_transcriber",
            lambda: _NoopTranscriber(),
        )
        monkeypatch.setattr(
            "app.routers.live_voice.select_turn_caller",
            lambda: _NoopTurnCaller(),
        )
        client = _client(monkeypatch, pool, auth_enabled=True)

        token_a = _token_for(_USER_A)
        url = _ws_url(_SESSION_A, token_a)

        with client.websocket_connect(url) as ws:
            frames, close_code = _drain_ws(ws, max_frames=6)

        phase_types = [f.get("type") for f in frames]

        assert "phase" in phase_types, f"expected phase frames, got {phase_types}"
        assert "ready" in phase_types, f"expected ready frame, got {phase_types}"

        # ready should come after all phase frames.
        ready_idx = phase_types.index("ready")
        phase_before = [t for t in phase_types[:ready_idx] if t == "phase"]
        assert len(phase_before) == 3, (
            f"expected 3 phase frames before ready, got {len(phase_before)}"
        )

        # After WS disconnect, verify status was flipped to 'active'.
        assert pool._conversations[_SESSION_A]["status"] == "active", (
            f"expected status='active', got {pool._conversations[_SESSION_A]['status']!r}"
        )

    def test_partner_receives_phase_frames(self, monkeypatch):
        """B connects to session where B is partner → normal frames, not 4003."""
        pool = LiveVoiceFakePool()
        _seed_ws_pool(pool)
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch, auth_enabled=True)
        monkeypatch.setattr(
            "app.routers.live_voice.select_transcriber",
            lambda: _NoopTranscriber(),
        )
        monkeypatch.setattr(
            "app.routers.live_voice.select_turn_caller",
            lambda: _NoopTurnCaller(),
        )
        client = _client(monkeypatch, pool, auth_enabled=True)

        token_b = _token_for(_USER_B)
        url = _ws_url(_SESSION_A, token_b)

        with client.websocket_connect(url) as ws:
            frames, close_code = _drain_ws(ws, max_frames=6)

        phase_types = [f.get("type") for f in frames]
        assert "phase" in phase_types, (
            f"partner should receive phase frames, got {phase_types}"
        )
        assert "ready" in phase_types, (
            f"partner should receive ready frame, got {phase_types}"
        )
        assert close_code != 4003, "partner should not get 4003 close"


class TestDevFallback:
    """With auth disabled, unauthenticated calls skip ownership checks."""

    def test_http_works_without_auth(self, monkeypatch):
        """Card + sessions endpoints work without auth when auth is disabled."""
        pool = LiveVoiceFakePool()
        _seed_ws_pool(pool)
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch, auth_enabled=False)
        client = _client(monkeypatch, pool, auth_enabled=False)

        resp = client.get(f"/api/live/sessions/{_SESSION_A}/card")
        assert resp.status_code == 200, resp.text

        resp2 = client.get("/api/live/sessions")
        assert resp2.status_code == 200, resp2.text

    def test_ws_works_without_auth(self, monkeypatch):
        """WS connects without token when auth is disabled."""
        pool = LiveVoiceFakePool()
        _seed_ws_pool(pool)
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch, auth_enabled=False)
        monkeypatch.setattr(
            "app.routers.live_voice.select_transcriber",
            lambda: _NoopTranscriber(),
        )
        monkeypatch.setattr(
            "app.routers.live_voice.select_turn_caller",
            lambda: _NoopTurnCaller(),
        )
        client = _client(monkeypatch, pool, auth_enabled=False)

        url = _ws_url(_SESSION_A, None)  # No token

        with client.websocket_connect(url) as ws:
            frames, close_code = _drain_ws(ws, max_frames=6)

        phase_types = [f.get("type") for f in frames]
        assert "ready" in phase_types, (
            f"dev fallback WS should receive ready frame, got {phase_types}"
        )
        assert pool._conversations[_SESSION_A]["status"] == "active", (
            "dev fallback should flip status to active"
        )


class TestCreateDiscoverStartRoundTrip:
    """Create→discover→start round trip as user A (integration test)."""

    def test_round_trip(self, monkeypatch):
        """Full create→discover→start round trip."""
        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch, auth_enabled=True)
        monkeypatch.setattr(
            "app.routers.live_voice.run_live_prep_agentic_job",
            _noop_ws_prep_job,
        )
        monkeypatch.setattr(
            "app.routers.live_voice.select_transcriber",
            lambda: _NoopTranscriber(),
        )
        monkeypatch.setattr(
            "app.routers.live_voice.select_turn_caller",
            lambda: _NoopTurnCaller(),
        )
        client = _client(monkeypatch, pool, auth_enabled=True)
        token_a = _token_for(_USER_A)

        # Step 1: Create session.
        create_resp = client.post(
            "/api/live/sessions",
            json={"bot_id": "tante_rosi", "steering_text": "round trip test"},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert create_resp.status_code == 200, create_resp.text
        session_id = create_resp.json()["session_id"]

        # Step 2: Discover — verify it appears in sessions list.
        list_resp = client.get(
            "/api/live/sessions",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert list_resp.status_code == 200, list_resp.text
        sessions = list_resp.json()["sessions"]
        found = [s for s in sessions if s["id"] == session_id]
        assert len(found) == 1, f"session {session_id} not in sessions list"

        # Step 3: Set ready and start via WS.
        pool._conversations[UUID(session_id)]["status"] = "ready"

        ws_url = f"/ws/live/{session_id}?token={token_a}"
        with client.websocket_connect(ws_url) as ws:
            frames, close_code = _drain_ws(ws, max_frames=6)

        phase_types = [f.get("type") for f in frames]
        assert "ready" in phase_types, (
            f"round-trip WS should reach ready, got {phase_types}"
        )

        # Step 4: Verify status flipped to 'active'.
        assert pool._conversations[UUID(session_id)]["status"] == "active", (
            "status should be 'active' after WS start"
        )

        # Step 5: Verify the session is still discoverable.
        list_resp2 = client.get(
            "/api/live/sessions",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        sessions2 = list_resp2.json()["sessions"]
        found2 = [s for s in sessions2 if s["id"] == session_id]
        assert len(found2) == 1
        assert found2[0]["status"] == "active"


# ═══════════════════════════════════════════════════════════════════════════════
# Stub helpers for WS tests
# ═══════════════════════════════════════════════════════════════════════════════

class _NoopTranscriber:
    """A transcriber that never emits events — blocks forward_events forever."""
    name = "stub"

    async def start(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    @property
    def events(self) -> "_NoopEventQueue":
        return _NoopEventQueue()


class _NoopEventQueue:
    """An async queue that never yields — blocks the forward_events() task."""
    async def get(self) -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}


class _NoopTurnCaller:
    pass


async def _noop_ws_prep_job(*args: Any, **kwargs: Any) -> None:
    pass
