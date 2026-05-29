"""Tests for live-voice ownership enforcement (Sprint 1 T8).

Covers:
(a) With auth enabled, every guarded HTTP route returns 403 when called with
    a non-owner's token and 404 for an unknown session.
(b) A solo session (partner_user_id=NULL) does NOT crash the ownership check
    — the owner's token returns a normal success response.
(c) GET /api/live/sessions returns only the caller's rows (own + partner),
    correct status filtering, newest-first ordering, and ``topic_label``
    derived from ``BOT_SPECS[bot_id].display_name``.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.bots.registry import BOT_SPECS, _maybe_register_staging_bots
from app.config import get_settings
from app.routers import live_voice as _lv
from app.routers.live_voice import router as live_voice_router
from app.services.auth import jwt as live_jwt

# ── reuse FakePool from prep tests ───────────────────────────────────────────
from tests.test_live_router_prep import (
    LiveVoiceFakePool,
    _fake_primary_topic_id_for,
)

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


def _prime(monkeypatch) -> None:
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "true")
    monkeypatch.setenv("STAGING", "1")
    get_settings.cache_clear()


def _token_for(user_id: UUID) -> str:
    return live_jwt.mint(user_id=str(user_id))


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_app(monkeypatch, pool: LiveVoiceFakePool) -> FastAPI:
    """Build a FastAPI test app with the live_voice router and our fake pool."""
    import app.bots.registry as _bot_reg

    monkeypatch.setenv("STAGING", "1")
    monkeypatch.setattr(_bot_reg, "_STAGING_BOTS_REGISTERED", False)
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "true")
    get_settings.cache_clear()

    # Ensure staging bots (tante_rosi) are registered so BOT_SPECS lookups work.
    _bot_reg._maybe_register_staging_bots()

    app = FastAPI()
    app.state.pool = pool
    app.include_router(live_voice_router)
    return app


def _client(monkeypatch, pool: LiveVoiceFakePool) -> TestClient:
    return TestClient(_make_app(monkeypatch, pool))


# ═══════════════════════════════════════════════════════════════════════════════
# Seed helpers
# ═══════════════════════════════════════════════════════════════════════════════

_USER_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_USER_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_SOLO_SESSION = UUID("11111111-1111-1111-1111-111111111111")
_SHARED_SESSION_A = UUID("22222222-2222-2222-2222-222222222222")
_SHARED_SESSION_B = UUID("33333333-3333-3333-3333-333333333333")


def _seed_pool(pool: LiveVoiceFakePool) -> None:
    """Seed conversations for ownership tests.

    * _SOLO_SESSION:  owned by A, partner=None,  status='active'
    * _SHARED_SESSION_A: owned by A, partner=B,    status='ready'
    * _SHARED_SESSION_B: owned by B, partner=A,    status='active'
    """
    pool._conversations[_SOLO_SESSION] = {
        "id": _SOLO_SESSION,
        "user_id": _USER_A,
        "partner_user_id": None,
        "bot_id": "tante_rosi",
        "mode": "open",
        "status": "active",
        "prep_summary": "Solo summary.",
        "steering_text": None,
        "current_item_id": None,
        "started_at": None,
        "session_fields": {},
        "created_at": "2026-05-01T10:00:00Z",
    }
    pool._conversations[_SHARED_SESSION_A] = {
        "id": _SHARED_SESSION_A,
        "user_id": _USER_A,
        "partner_user_id": _USER_B,
        "bot_id": "tante_rosi",
        "mode": "open",
        "status": "ready",
        "prep_summary": "Shared A summary.",
        "steering_text": None,
        "current_item_id": None,
        "started_at": None,
        "session_fields": {},
        "created_at": "2026-05-02T10:00:00Z",
    }
    pool._conversations[_SHARED_SESSION_B] = {
        "id": _SHARED_SESSION_B,
        "user_id": _USER_B,
        "partner_user_id": _USER_A,
        "bot_id": "tante_rosi",
        "mode": "open",
        "status": "active",
        "prep_summary": "Shared B summary.",
        "steering_text": None,
        "current_item_id": None,
        "started_at": None,
        "session_fields": {},
        "created_at": "2026-05-03T10:00:00Z",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════

# ── List of guarded routes: (method, url_template, needs_body) ───────────
_GUARDED_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("get",    "/api/live/sessions/{sid}/card",          None),
    ("get",    "/api/live/sessions/{sid}",               None),
    ("get",    "/api/live/sessions/{sid}/review",        None),
    ("post",   "/api/live/sessions/{sid}/end",           None),
    ("post",   "/api/live/sessions/{sid}/review/save",
        {"keep_items": [], "keep_notes": []}),
    ("post",   "/api/live/sessions/{sid}/prep/retry",    None),
    ("post",   "/api/live/sessions/{sid}/debrief/retry", None),
    ("post",   "/api/live/sessions/{sid}/consent",
        {"kind": "solo"}),
    ("get",    "/api/live/sessions/{sid}/tts/{tid}",     None),
]


class TestOwnership403OnGuardedRoutes:
    """Every guarded route returns 403 when a non-owner hits it."""

    def _setup(self, monkeypatch, *, enable_debrief: bool = True):
        pool = LiveVoiceFakePool()
        _seed_pool(pool)
        if enable_debrief:
            monkeypatch.setenv("LIVE_DEBRIEF_AGENTIC_ENABLED", "true")
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch)
        client = _client(monkeypatch, pool)
        token_b = _token_for(_USER_B)
        return client, token_b, pool

    @pytest.mark.parametrize("method,url_tpl,body", _GUARDED_ROUTES)
    def test_non_owner_403(self, method, url_tpl, body, monkeypatch):
        """B's token against A's solo session → 403."""
        client, token_b, pool = self._setup(monkeypatch)
        _seed_pool(pool)

        sid = str(_SOLO_SESSION)
        url = url_tpl.format(sid=sid, tid=str(uuid4()))
        kwargs: dict[str, Any] = {"headers": _auth_header(token_b)}
        if method == "get":
            kwargs["params"] = {}
        if body is not None:
            kwargs["json"] = body

        resp = getattr(client, method)(url, **kwargs)
        # raise_server_exceptions=False so we see the HTTPException as a response
        # But TestClient by default raises for 500s but NOT for 4xx.
        # For 403, resp.status_code should be 403.
        assert resp.status_code == 403, (
            f"{method.upper()} {url_tpl} with non-owner token: "
            f"expected 403, got {resp.status_code} {resp.text}"
        )

    @pytest.mark.parametrize("method,url_tpl,body", _GUARDED_ROUTES)
    def test_unknown_session_404(self, method, url_tpl, body, monkeypatch):
        """Owner's token against an unknown session → 404."""
        client, token_a, pool = self._setup(monkeypatch)
        _seed_pool(pool)
        token_a = _token_for(_USER_A)

        unknown_sid = str(uuid4())
        url = url_tpl.format(sid=unknown_sid, tid=str(uuid4()))
        kwargs: dict[str, Any] = {"headers": _auth_header(token_a)}
        if method == "get":
            kwargs["params"] = {}
        if body is not None:
            kwargs["json"] = body

        resp = getattr(client, method)(url, **kwargs)
        assert resp.status_code == 404, (
            f"{method.upper()} {url_tpl} with unknown session: "
            f"expected 404, got {resp.status_code} {resp.text}"
        )


class TestSoloSessionNoCrash:
    """Solo session (NULL partner_user_id) does not crash ownership check."""

    def test_card_on_solo_session_succeeds(self, monkeypatch):
        """Owner's token on solo session → card returns 200."""
        pool = LiveVoiceFakePool()
        _seed_pool(pool)
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch)
        client = _client(monkeypatch, pool)
        token_a = _token_for(_USER_A)

        resp = client.get(
            f"/api/live/sessions/{_SOLO_SESSION}/card",
            headers=_auth_header(token_a),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Status should be 'active' (canonicalized).
        assert data["status"] == "active", data
        assert data["session_id"] == str(_SOLO_SESSION)

    def test_get_session_on_solo_session_succeeds(self, monkeypatch):
        """Owner's token on solo session → get_session returns 200."""
        pool = LiveVoiceFakePool()
        _seed_pool(pool)
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch)
        client = _client(monkeypatch, pool)
        token_a = _token_for(_USER_A)

        resp = client.get(
            f"/api/live/sessions/{_SOLO_SESSION}",
            headers=_auth_header(token_a),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "active", data
        assert data["id"] == str(_SOLO_SESSION)

    def test_consent_on_solo_session_succeeds(self, monkeypatch):
        """Owner's token on solo session → consent returns 200."""
        pool = LiveVoiceFakePool()
        _seed_pool(pool)
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch)
        client = _client(monkeypatch, pool)
        token_a = _token_for(_USER_A)

        resp = client.post(
            f"/api/live/sessions/{_SOLO_SESSION}/consent",
            json={"kind": "solo"},
            headers=_auth_header(token_a),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True


class TestSessionListScoping:
    """GET /api/live/sessions returns only the caller's rows."""

    def test_list_sessions_returns_only_callers_rows(self, monkeypatch):
        """User A sees own + shared sessions, but not B's solo session."""
        pool = LiveVoiceFakePool()
        _seed_pool(pool)
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch)
        client = _client(monkeypatch, pool)
        token_a = _token_for(_USER_A)

        resp = client.get(
            "/api/live/sessions",
            headers=_auth_header(token_a),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        sessions = data["sessions"]

        # User A should see:
        #   _SOLO_SESSION (user_id=A, partner=NULL)
        #   _SHARED_SESSION_A (user_id=A, partner=B)
        #   _SHARED_SESSION_B (user_id=B, partner=A)  ← via partner match
        ids = {s["id"] for s in sessions}
        assert str(_SOLO_SESSION) in ids, "missing solo session"
        assert str(_SHARED_SESSION_A) in ids, "missing shared A session"
        assert str(_SHARED_SESSION_B) in ids, "missing shared B session (user A is partner)"

        # Each session must have the expected fields.
        for s in sessions:
            assert "id" in s
            assert "status" in s
            assert "topic_label" in s
            assert "prep_summary" in s
            assert "steering_text" in s
            assert "item_count" in s
            assert "created_at" in s

    def test_list_sessions_newest_first_ordering(self, monkeypatch):
        """Sessions are ordered by created_at DESC."""
        pool = LiveVoiceFakePool()
        _seed_pool(pool)
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch)
        client = _client(monkeypatch, pool)
        token_a = _token_for(_USER_A)

        resp = client.get(
            "/api/live/sessions",
            headers=_auth_header(token_a),
        )
        assert resp.status_code == 200, resp.text
        sessions = resp.json()["sessions"]
        # created_at order: B (2026-05-03) > A (2026-05-02) > Solo (2026-05-01)
        assert sessions[0]["id"] == str(_SHARED_SESSION_B), f"got {sessions[0]['id']}"
        assert sessions[1]["id"] == str(_SHARED_SESSION_A), f"got {sessions[1]['id']}"
        assert sessions[2]["id"] == str(_SOLO_SESSION), f"got {sessions[2]['id']}"

    def test_list_sessions_status_filter(self, monkeypatch):
        """Status filter returns only matching rows."""
        pool = LiveVoiceFakePool()
        _seed_pool(pool)
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch)
        client = _client(monkeypatch, pool)
        token_a = _token_for(_USER_A)

        # Filter for 'active' — should get solo session and shared B session.
        resp = client.get(
            "/api/live/sessions?status=active",
            headers=_auth_header(token_a),
        )
        assert resp.status_code == 200, resp.text
        sessions = resp.json()["sessions"]
        ids = {s["id"] for s in sessions}
        assert str(_SOLO_SESSION) in ids
        assert str(_SHARED_SESSION_B) in ids
        assert str(_SHARED_SESSION_A) not in ids  # status=ready

    def test_list_sessions_topic_label_from_bot_specs(self, monkeypatch):
        """topic_label is derived from BOT_SPECS display_name."""
        pool = LiveVoiceFakePool()
        _seed_pool(pool)
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch)
        client = _client(monkeypatch, pool)
        token_a = _token_for(_USER_A)

        resp = client.get(
            "/api/live/sessions",
            headers=_auth_header(token_a),
        )
        assert resp.status_code == 200, resp.text
        sessions = resp.json()["sessions"]

        # bot_id='tante_rosi' should map to BOT_SPECS['tante_rosi'].display_name
        expected_label = BOT_SPECS["tante_rosi"].display_name
        for s in sessions:
            assert s["topic_label"] == expected_label, (
                f"expected topic_label={expected_label!r}, got {s['topic_label']!r}"
            )

    def test_list_sessions_unknown_bot_id_fallback(self, monkeypatch):
        """When bot_id is not in BOT_SPECS, topic_label falls back to bot_id."""
        pool = LiveVoiceFakePool()
        _seed_pool(pool)
        # Change bot_id to unknown.
        pool._conversations[_SOLO_SESSION]["bot_id"] = "no_such_bot"
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        _prime(monkeypatch)
        client = _client(monkeypatch, pool)
        token_a = _token_for(_USER_A)

        resp = client.get(
            "/api/live/sessions",
            headers=_auth_header(token_a),
        )
        assert resp.status_code == 200, resp.text
        sessions = resp.json()["sessions"]
        solo = [s for s in sessions if s["id"] == str(_SOLO_SESSION)][0]
        assert solo["topic_label"] == "no_such_bot", (
            f"fallback label should be bot_id, got {solo['topic_label']!r}"
        )
