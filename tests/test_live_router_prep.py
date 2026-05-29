"""Sprint 2 — Router tests for live-prep async session lifecycle.

Covers:
(a) POST /api/live/sessions returns immediately with status='prepping' and prep_pending=true.
(b) /card while prepping returns empty items with prep_pending=true.
(c) Drive scheduled task to completion (await the task directly), assert /card flips to ready with items.
(d) /card while prep_failed returns failure reason.
(e) /prep/retry returns 409 for non-prep_failed sessions.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.bots.registry import primary_topic_id_for
from app.config import get_settings
from app.routers.live_voice import router as live_voice_router


# --------------------------------------------------------------------------- #
# FakePool for live-voice router tests
# --------------------------------------------------------------------------- #

_RELATIONSHIP_TOPIC_ID = UUID("00000000-0000-4000-8000-000000000001")


class _FakeTxn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, parent: "_LiveVoiceFakePool") -> None:
        self._parent = parent

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def execute(self, sql: str, *args: Any) -> str:
        return await self._parent.execute(sql, *args)


class _FakeAcquire:
    def __init__(self, parent: "_LiveVoiceFakePool") -> None:
        self._parent = parent

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._parent)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class LiveVoiceFakePool:
    """Minimal fake pool for live_voice router tests.

    Handles the exact SQL patterns used by create_session, get_session_card,
    and retry_prep endpoints.
    """

    def __init__(self) -> None:
        # conversation rows keyed by UUID
        self._conversations: dict[UUID, dict[str, Any]] = {}
        # conversation_items keyed by (conversation_id, item_id)
        self._items: dict[UUID, list[dict[str, Any]]] = {}
        # Track executed SQL for assertions
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        # conversation_table_exists flag
        self._conversations_table_exists: bool = True
        # topic_id returned by primary_topic_id_for (mocked via monkeypatch)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        compact = " ".join(sql.split())
        if "information_schema.tables" in compact and "conversations" in compact:
            return self._conversations_table_exists
        raise AssertionError(f"unexpected fetchval: {compact}")

    def _resolve_uuid(self, value: Any) -> Any:
        """Coerce a string argument to UUID if it looks like one."""
        if isinstance(value, str):
            try:
                return UUID(value)
            except (ValueError, AttributeError):
                pass
        return value

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        compact = " ".join(sql.split())
        # SELECT from mediator.conversations (used by card and retry endpoints)
        if compact.startswith("SELECT id, user_id, bot_id, mode, status, prep_summary"):
            return self._conversations.get(self._resolve_uuid(args[0]))
        if compact.startswith("SELECT id, status FROM mediator.conversations WHERE id"):
            conv = self._conversations.get(self._resolve_uuid(args[0]))
            if conv is None:
                return None
            return {"id": conv["id"], "status": conv["status"]}
        # SELECT with user_id, bot_id, steering_text, topic_id, status,
        # session_fields (used by retry_live_prep).
        if compact.startswith("SELECT id, user_id, bot_id, steering_text, topic_id, status, session_fields"):
            conv = self._conversations.get(self._resolve_uuid(args[0]))
            if conv is None:
                return None
            return {
                "id": conv["id"],
                "user_id": conv.get("user_id", str(uuid4())),
                "bot_id": conv.get("bot_id", "unknown"),
                "steering_text": conv.get("steering_text", ""),
                "topic_id": conv.get("topic_id"),
                "status": conv["status"],
                "session_fields": conv.get("session_fields", {}),
            }
        # _require_ownership (HTTP): SELECT id, user_id, partner_user_id, status FROM mediator.conversations WHERE id
        if compact.startswith("SELECT id, user_id, partner_user_id, status FROM mediator.conversations WHERE id"):
            conv = self._conversations.get(self._resolve_uuid(args[0]))
            if conv is None:
                return None
            return {
                "id": conv["id"],
                "user_id": conv.get("user_id"),
                "partner_user_id": conv.get("partner_user_id", None),
                "status": conv["status"],
            }
        # WS ownership check: SELECT user_id, partner_user_id FROM mediator.conversations WHERE id
        if compact.startswith("SELECT user_id, partner_user_id FROM mediator.conversations WHERE id"):
            conv = self._conversations.get(self._resolve_uuid(args[0]))
            if conv is None:
                return None
            return {
                "user_id": conv.get("user_id"),
                "partner_user_id": conv.get("partner_user_id", None),
            }
        # get_session (legacy): SELECT * FROM mediator.conversations WHERE id
        if compact.startswith("SELECT * FROM mediator.conversations WHERE id"):
            return self._conversations.get(self._resolve_uuid(args[0]))
        # gather_prep_context: SELECT from users
        if compact.startswith("SELECT id, name, phone, timezone, style_notes, onboarding_state"):
            return {
                "id": args[0],
                "name": "Test User",
                "timezone": "America/New_York",
                "style_notes": None,
                "onboarding_state": "done",
                "pacing_preferences": None,
                "pregnancy_edd": None,
                "pregnancy_dating_basis": None,
                "pregnancy_lmp_date": None,
                "pregnancy_scan_date": None,
                "pregnancy_scan_corrected_at": None,
                "pregnancy_started_at": None,
                "pregnancy_ended_at": None,
                "pregnancy_outcome": None,
            }
        raise AssertionError(f"unexpected fetchrow: {compact}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        compact = " ".join(sql.split())
        # list_sessions: SELECT id, status, bot_id, prep_summary, steering_text, created_at, ... FROM mediator.conversations c WHERE (user_id = $1 OR partner_user_id = $1)
        if compact.startswith("SELECT id, status, bot_id, prep_summary, steering_text, created_at"):
            user_id = self._resolve_uuid(args[0]) if args else None
            status_filter = args[1] if len(args) > 1 else None
            results = []
            for conv in self._conversations.values():
                uid = conv.get("user_id")
                pid = conv.get("partner_user_id")
                if uid != user_id and pid != user_id:
                    continue
                if status_filter is not None and conv.get("status") != status_filter:
                    continue
                results.append({
                    "id": conv["id"],
                    "status": conv["status"],
                    "bot_id": conv.get("bot_id", "unknown"),
                    "prep_summary": conv.get("prep_summary"),
                    "steering_text": conv.get("steering_text"),
                    "created_at": conv.get("created_at"),
                    "item_count": len(self._items.get(conv["id"], [])),
                })
            return sorted(results, key=lambda r: (r["created_at"] or ""), reverse=True)
        if "FROM mediator.conversation_items" in compact:
            conv_id = self._resolve_uuid(args[0]) if args else None
            return self._items.get(conv_id, [])
        # gather_prep_context: SELECT from themes (id, slug, label)
        if "FROM themes" in compact and "slug = ANY" not in compact:
            return []
        # produce_agenda theme lookup: SELECT id, slug FROM themes WHERE ...
        if "FROM themes" in compact and "slug = ANY" in compact:
            return []
        # gather_prep_context: SELECT from distillations
        if "FROM distillations" in compact:
            return []
        raise AssertionError(f"unexpected fetch: {compact}")

    async def execute(self, sql: str, *args: Any) -> str:
        compact = " ".join(sql.split())
        self.executed.append((compact, args))

        # INSERT INTO mediator.conversations
        # The current INSERT embeds literals for status/prep_summary/current_item_id:
        #   VALUES ($1, $2, $3, $4, $5, $6, 'preparing', NULL, NULL, $7)
        # So args = [id, user_id, partner_user_id, bot_id, mode, steering_text, topic_id]
        if compact.startswith("INSERT INTO mediator.conversations"):
            row_id = args[0] if args else uuid4()
            row: dict[str, Any] = {
                "id": row_id,
                "status": "preparing",       # canonical default
                "prep_summary": None,        # always literal NULL
                "current_item_id": None,     # always literal NULL
                "session_fields": {},
                "steering_text": None,
            }
            if len(args) >= 2:
                row["user_id"] = args[1]
            if len(args) >= 3:
                row["partner_user_id"] = args[2]
            if len(args) >= 4:
                row["bot_id"] = args[3]
            if len(args) >= 5:
                row["mode"] = args[4]
            if len(args) >= 6:
                row["steering_text"] = args[5]
            # args[6] (if present) is topic_id, NOT status
            if len(args) >= 7 and args[6] is not None:
                row["topic_id"] = args[6]
            self._conversations[row_id] = row
            return "INSERT 0 1"

        # UPDATE mediator.conversations SET status = ...
        if compact.startswith(
            "UPDATE mediator.conversations SET status = 'preparing' WHERE"
        ) or compact.startswith(
            "UPDATE mediator.conversations SET status = 'prepping' WHERE"
        ):
            conv_id = self._resolve_uuid(args[0])
            if conv_id in self._conversations:
                self._conversations[conv_id]["status"] = "preparing"
            return "UPDATE 1"

        if compact.startswith("UPDATE mediator.conversations SET status = 'ready'"):
            conv_id = self._resolve_uuid(args[0])
            if conv_id in self._conversations:
                self._conversations[conv_id]["status"] = "ready"
            return "UPDATE 1"

        if compact.startswith("UPDATE mediator.conversations SET status = 'prep_failed'"):
            conv_id = self._resolve_uuid(args[0])
            if conv_id in self._conversations:
                self._conversations[conv_id]["status"] = "prep_failed"
            return "UPDATE 1"

        # Generic UPDATE with SET ... WHERE id = $1 (multiple params)
        if compact.startswith("UPDATE mediator.conversations SET"):
            # Try to find the id param — arg can be UUID, UUID str, or other.
            # Prefer the first arg that matches a known conversation.
            for i, arg in enumerate(args):
                maybe_uuid = self._resolve_uuid(arg)
                if isinstance(maybe_uuid, UUID):
                    conv_id = maybe_uuid
                    if conv_id in self._conversations:
                        # Parse SET clauses from compact string
                        if "status = " in compact:
                            import re as _re
                            m = _re.search(r"status = '(\w+)'", compact)
                            if m:
                                self._conversations[conv_id]["status"] = m.group(1)
                        if "session_fields" in compact and "jsonb_build_object" in compact:
                            self._conversations[conv_id].setdefault("session_fields", {})
                            if "retry_count" in compact:
                                self._conversations[conv_id]["session_fields"]["retry_count"] = args[1] if len(args) > 1 else 1
                            else:
                                self._conversations[conv_id]["session_fields"]["prep_error"] = "orphaned"
                        return "UPDATE 1"
            return "UPDATE 1"

        # INSERT INTO mediator.bot_turns
        if compact.startswith("INSERT INTO mediator.bot_turns"):
            return "INSERT 0 1"

        # INSERT INTO mediator.conversation_items
        if compact.startswith("INSERT INTO mediator.conversation_items"):
            # args: id, conversation_id, title, intent, ask, done_when, kind, priority, speaker_scope, theme_id, order_hint, coverage_evidence_required
            conv_id = self._resolve_uuid(args[1]) if len(args) > 1 else None
            item_id = args[0] if args else uuid4()
            if conv_id is not None:
                self._items.setdefault(conv_id, []).append({
                    "id": item_id,
                    "conversation_id": conv_id,
                    "title": args[2] if len(args) > 2 else "",
                    "intent": args[3] if len(args) > 3 else None,
                    "ask": args[4] if len(args) > 4 else None,
                    "done_when": args[5] if len(args) > 5 else None,
                    "kind": args[6] if len(args) > 6 else "talk",
                    "priority": args[7] if len(args) > 7 else "should",
                    "speaker_scope": args[8] if len(args) > 8 else "any",
                    "theme_id": args[9] if len(args) > 9 else None,
                    "order_hint": args[10] if len(args) > 10 else 0,
                    "coverage_evidence_required": args[11] if len(args) > 11 else False,
                })
            return "INSERT 0 1"

        # UPDATE mediator.conversations SET current_item_id
        if "current_item_id" in compact:
            conv_id = args[0]
            if conv_id in self._conversations:
                self._conversations[conv_id]["current_item_id"] = args[0]
            return "UPDATE 1"

        # UPDATE mediator.conversations SET prep_summary
        if "prep_summary" in compact:
            conv_id = args[0] if len(args) > 1 else None
            if conv_id is not None and conv_id in self._conversations:
                self._conversations[conv_id]["prep_summary"] = args[1] if len(args) > 1 else "prepped"
            return "UPDATE 1"

        # INSERT / UPDATE for artifacts, artifact_links
        if "conversation_artifacts" in compact or "artifact_links" in compact:
            return "INSERT 0 1"

        # Bot turns finalize
        if "bot_turns" in compact and "UPDATE" in compact:
            return "UPDATE 1"

        return "OK"


# --------------------------------------------------------------------------- #
# FastAPI test app factory
# --------------------------------------------------------------------------- #


def _make_app(monkeypatch, pool: LiveVoiceFakePool) -> FastAPI:
    """Build a FastAPI test app with the live_voice router and our fake pool."""
    # Ensure staging bots are registered so 'tante_rosi' is available.
    monkeypatch.setenv("STAGING", "1")
    # Reset the staging-registered sentinel so _maybe_register_staging_bots
    # re-runs (it short-circuits after first call regardless of STAGING).
    import app.bots.registry as _bot_reg
    monkeypatch.setattr(_bot_reg, "_STAGING_BOTS_REGISTERED", False)

    # Ensure settings are primed with required env vars
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:***@localhost:5432/db")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "dummy-service-role")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai")
    monkeypatch.setenv("GROQ_API_KEY", "dummy-groq")
    monkeypatch.setenv("WHATSAPP_TOKEN", "dummy-whatsapp")
    monkeypatch.setenv("WHATSAPP_BEARER_TOKEN", "dummy-whatsapp")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "12345")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "dummy-verify")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "dummy-secret")
    monkeypatch.setenv("ADMIN_PASSWORD", "dummy-admin")
    monkeypatch.setenv("PARTNER_PHONE_A", "15555550100")
    monkeypatch.setenv("PARTNER_PHONE_B", "15555550101")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "")
    monkeypatch.setenv("SUPABASE_STORAGE_BUCKET", "mediator-media")
    monkeypatch.setenv("MEDIA_FETCH_TIMEOUT_S", "30")
    monkeypatch.setenv("DEFAULT_USER_TIMEZONE", "UTC")
    get_settings.cache_clear()

    app = FastAPI()
    app.state.pool = pool
    app.include_router(live_voice_router)
    return app


def _client(monkeypatch, pool: LiveVoiceFakePool) -> TestClient:
    return TestClient(_make_app(monkeypatch, pool))


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class TestSessionCreation:
    """POST /api/live/sessions returns immediately with status='preparing'."""

    def test_create_session_returns_prepping_immediately(self, monkeypatch) -> None:
        """(a) POST returns status='preparing' and prep_pending=true."""
        pool = LiveVoiceFakePool()
        # Patch primary_topic_id_for to return a known topic id.
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        # Prevent background prep from running and crashing.
        monkeypatch.setattr(
            "app.routers.live_voice.run_live_prep_agentic_job",
            _noop_prep_job,
        )
        client = _client(monkeypatch, pool)

        resp = client.post(
            "/api/live/sessions",
            json={"bot_id": "tante_rosi", "steering_text": "test prep"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "preparing", data
        assert data["prep_pending"] is True, data
        assert "session_id" in data

        # Verify the conversation row was inserted in preparing status.
        session_id = UUID(data["session_id"])
        assert session_id in pool._conversations
        # The noop prep leaves the conversation in preparing (it's a no-op).
        # The background task may have run, but since we patched it, status
        # should remain as originally set ("preparing").
        assert pool._conversations[session_id]["status"] == "preparing"

    def test_create_session_stub_path_returns_ready(self, monkeypatch) -> None:
        """With LIVE_VOICE_PREP_PROVIDER=stub, legacy sync path returns ready."""
        monkeypatch.setenv("LIVE_VOICE_PREP_PROVIDER", "stub")
        get_settings.cache_clear()

        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        client = _client(monkeypatch, pool)

        resp = client.post(
            "/api/live/sessions",
            json={"bot_id": "tante_rosi", "steering_text": "test"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ready", data
        assert data["prep_pending"] is False, data

    def test_create_session_skip_prep_returns_ready_without_background_job(
        self,
        monkeypatch,
    ) -> None:
        """Just-speak mode skips prep and opens the consent/live flow directly."""

        async def _should_not_run(*args, **kwargs) -> None:
            raise AssertionError("skip_prep must not schedule live prep")

        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        monkeypatch.setattr(
            "app.routers.live_voice.run_live_prep_agentic_job",
            _should_not_run,
        )
        client = _client(monkeypatch, pool)

        resp = client.post(
            "/api/live/sessions",
            json={
                "bot_id": "tante_rosi",
                "steering_text": "",
                "skip_prep": True,
            },
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ready", data
        assert data["prep_pending"] is False, data
        session_id = UUID(data["session_id"])
        assert pool._conversations[session_id]["status"] == "ready"


class TestCardEndpoint:
    """GET /api/live/sessions/{id}/card returns correct states."""

    def test_card_while_prepping_returns_pending(self, monkeypatch) -> None:
        """(b) /card while preparing returns empty items with prep_pending=true."""
        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        # Prevent background prep from running and crashing.
        monkeypatch.setattr(
            "app.routers.live_voice.run_live_prep_agentic_job",
            _noop_prep_job,
        )
        client = _client(monkeypatch, pool)

        # Create a session (async path — returns preparing).
        resp = client.post(
            "/api/live/sessions",
            json={"bot_id": "tante_rosi"},
        )
        session_id = resp.json()["session_id"]

        # Hit /card — should be preparing.
        card = client.get(f"/api/live/sessions/{session_id}/card")
        assert card.status_code == 200, card.text
        card_data = card.json()
        assert card_data["status"] == "preparing", card_data
        assert card_data["prep_pending"] is True, card_data
        assert card_data["items"] == [], card_data
        assert card_data["failure_reason"] is None, card_data

    @pytest.mark.anyio
    async def test_card_after_ready_returns_items(self, monkeypatch) -> None:
        """(c) After driving the task, /card flips to ready with items."""
        from httpx import ASGITransport, AsyncClient

        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )

        # Monkeypatch run_live_prep_agentic_job to simulate successful prep.
        async def _fake_prep_job(
            conversation_id, user_id, bot_id, steering_text, topic_id, pool,
        ):
            # Simulate successful prep: set status ready, insert items.
            # The 'pool' parameter shadows the outer 'pool' — both refer to
            # the same LiveVoiceFakePool instance.
            pool._conversations[conversation_id]["status"] = "ready"
            # Add a couple of conversation items
            pool._items[conversation_id] = [
                {
                    "id": uuid4(),
                    "conversation_id": conversation_id,
                    "title": "Check-in",
                    "intent": "Open the conversation",
                    "ask": "How are things?",
                    "done_when": "Partner responds",
                    "kind": "talk",
                    "priority": "must",
                    "speaker_scope": "any",
                    "theme_id": None,  # no theme lookup needed
                    "theme_label": None,
                    "order_hint": 0,
                    "coverage_evidence_required": False,
                },
                {
                    "id": uuid4(),
                    "conversation_id": conversation_id,
                    "title": "Follow-up",
                    "intent": "Dig deeper",
                    "ask": "Tell me more",
                    "done_when": "Natural close",
                    "kind": "talk",
                    "priority": "should",
                    "speaker_scope": "any",
                    "theme_id": None,
                    "theme_label": None,
                    "order_hint": 1,
                    "coverage_evidence_required": False,
                },
            ]

        monkeypatch.setattr(
            "app.routers.live_voice.run_live_prep_agentic_job",
            _fake_prep_job,
        )

        app = _make_app(monkeypatch, pool)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            # Create session — should return preparing.
            resp = await client.post(
                "/api/live/sessions",
                json={"bot_id": "tante_rosi", "steering_text": "test"},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            session_id = data["session_id"]
            assert data["status"] == "preparing"

            # The background task was created via asyncio.create_task.
            # Give the event loop a tick to process pending tasks.
            await asyncio.sleep(0)

            # Now /card should show ready with items.
            card = await client.get(
                f"/api/live/sessions/{session_id}/card"
            )
            assert card.status_code == 200, card.text
            card_data = card.json()
            assert card_data["status"] == "ready", card_data
            assert card_data["prep_pending"] is False, card_data
            assert len(card_data["items"]) == 2, card_data
            assert card_data["failure_reason"] is None, card_data

    def test_card_while_prep_failed_returns_failure_reason(self, monkeypatch) -> None:
        """(d) /card while prep_failed returns failure reason."""
        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        client = _client(monkeypatch, pool)

        # Insert a conversation directly in prep_failed state.
        failed_id = uuid4()
        pool._conversations[failed_id] = {
            "id": failed_id,
            "user_id": UUID("00000000-0000-0000-0000-000000000001"),
            "bot_id": "tante_rosi",
            "mode": "open",
            "status": "prep_failed",
            "prep_summary": None,
            "current_item_id": None,
            "steering_text": None,
            "session_fields": {"prep_error": "test failure reason"},
            "topic_id": _RELATIONSHIP_TOPIC_ID,
        }

        card = client.get(f"/api/live/sessions/{failed_id}/card")
        assert card.status_code == 200, card.text
        card_data = card.json()
        assert card_data["status"] == "prep_failed", card_data
        assert card_data["prep_pending"] is False, card_data
        assert card_data["items"] == [], card_data
        assert card_data["failure_reason"] == "test failure reason", card_data


class TestRetryEndpoint:
    """POST /api/live/sessions/{id}/prep/retry."""

    def test_retry_returns_409_for_non_prep_failed(self, monkeypatch) -> None:
        """(e) /prep/retry returns 409 for non-prep_failed sessions."""
        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        client = _client(monkeypatch, pool)

        # Insert a ready session.
        ready_id = uuid4()
        pool._conversations[ready_id] = {
            "id": ready_id,
            "user_id": UUID("00000000-0000-0000-0000-000000000001"),
            "bot_id": "tante_rosi",
            "mode": "open",
            "status": "ready",
            "prep_summary": None,
            "current_item_id": None,
            "steering_text": None,
            "session_fields": {},
            "topic_id": _RELATIONSHIP_TOPIC_ID,
        }

        resp = client.post(f"/api/live/sessions/{ready_id}/prep/retry")
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "only 'prep_failed'" in detail.lower() or "prep_failed" in detail.lower()

    def test_retry_returns_409_for_prepping(self, monkeypatch) -> None:
        """Retry on a prepping session also returns 409."""
        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        client = _client(monkeypatch, pool)

        prepping_id = uuid4()
        pool._conversations[prepping_id] = {
            "id": prepping_id,
            "user_id": UUID("00000000-0000-0000-0000-000000000001"),
            "bot_id": "tante_rosi",
            "mode": "open",
            "status": "prepping",
            "prep_summary": None,
            "current_item_id": None,
            "steering_text": None,
            "session_fields": {},
            "topic_id": _RELATIONSHIP_TOPIC_ID,
        }

        resp = client.post(f"/api/live/sessions/{prepping_id}/prep/retry")
        assert resp.status_code == 409, resp.text

    def test_retry_succeeds_for_prep_failed(self, monkeypatch) -> None:
        """Retry on a prep_failed session returns 200 with status='preparing'."""
        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        client = _client(monkeypatch, pool)

        failed_id = uuid4()
        pool._conversations[failed_id] = {
            "id": failed_id,
            "user_id": UUID("00000000-0000-0000-0000-000000000001"),
            "bot_id": "tante_rosi",
            "mode": "open",
            "status": "prep_failed",
            "prep_summary": None,
            "current_item_id": None,
            "steering_text": None,
            "session_fields": {"prep_error": "test failure"},
            "topic_id": _RELATIONSHIP_TOPIC_ID,
        }

        resp = client.post(f"/api/live/sessions/{failed_id}/prep/retry")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "preparing", data
        assert data["prep_pending"] is True, data


class TestNotFoundAndErrors:
    """Edge cases: 404 for missing sessions, 503 for missing table."""

    def test_card_returns_404_for_unknown_session(self, monkeypatch) -> None:
        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        client = _client(monkeypatch, pool)

        resp = client.get(f"/api/live/sessions/{uuid4()}/card")
        assert resp.status_code == 404, resp.text

    def test_retry_returns_404_for_unknown_session(self, monkeypatch) -> None:
        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        client = _client(monkeypatch, pool)

        resp = client.post(f"/api/live/sessions/{uuid4()}/prep/retry")
        assert resp.status_code == 404, resp.text

    def test_conversations_table_missing_gives_503(self, monkeypatch) -> None:
        pool = LiveVoiceFakePool()
        pool._conversations_table_exists = False
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        client = _client(monkeypatch, pool)

        resp = client.post(
            "/api/live/sessions",
            json={"bot_id": "tante_rosi"},
        )
        assert resp.status_code == 503, resp.text


class TestSingleRowCreation:
    """T3: Both async and producer/stub prep paths create exactly one
    conversation row — no orphan rows."""

    def test_stub_path_creates_exactly_one_row(self, monkeypatch) -> None:
        """LIVE_VOICE_PREP_PROVIDER=stub creates exactly one conversation row.

        The router pre-inserts a row in 'preparing' status, then passes
        the session_id to produce_agenda which updates it in-place to
        'ready'.  No second row is inserted.
        """
        monkeypatch.setenv("LIVE_VOICE_PREP_PROVIDER", "stub")
        get_settings.cache_clear()

        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        client = _client(monkeypatch, pool)

        resp = client.post(
            "/api/live/sessions",
            json={"bot_id": "tante_rosi", "steering_text": "test"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ready"
        assert data["prep_pending"] is False

        # Exactly one conversation row must exist.
        assert len(pool._conversations) == 1, (
            f"Expected exactly 1 conversation row, "
            f"found {len(pool._conversations)}: {list(pool._conversations.keys())}"
        )

    def test_async_path_creates_exactly_one_row(self, monkeypatch) -> None:
        """Default async path creates exactly one conversation row.

        The router inserts one row in 'preparing' status and schedules
        background prep.  No second row should ever be inserted.
        """
        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        monkeypatch.setattr(
            "app.routers.live_voice.run_live_prep_agentic_job",
            _noop_prep_job,
        )
        client = _client(monkeypatch, pool)

        resp = client.post(
            "/api/live/sessions",
            json={"bot_id": "tante_rosi"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["session_id"]

        # Exactly one conversation row must exist.
        assert len(pool._conversations) == 1, (
            f"Expected exactly 1 conversation row, "
            f"found {len(pool._conversations)}: {list(pool._conversations.keys())}"
        )

    def test_router_returns_preinserted_session_id(self, monkeypatch) -> None:
        """The router returns the same session_id it pre-inserted, not a new one."""
        monkeypatch.setenv("LIVE_VOICE_PREP_PROVIDER", "stub")
        get_settings.cache_clear()

        pool = LiveVoiceFakePool()
        monkeypatch.setattr(
            "app.routers.live_voice.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        client = _client(monkeypatch, pool)

        resp = client.post(
            "/api/live/sessions",
            json={"bot_id": "tante_rosi", "steering_text": "test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        session_id_from_response = UUID(data["session_id"])

        # The only row in the pool should have this ID.
        assert session_id_from_response in pool._conversations, (
            f"Response session_id={session_id_from_response} not found in "
            f"pool conversations: {list(pool._conversations.keys())}"
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _fake_primary_topic_id_for(pool, bot_spec):
    """Return a stable topic UUID so the router can resolve topic_id."""
    return _RELATIONSHIP_TOPIC_ID


async def _noop_prep_job(
    conversation_id, user_id, bot_id, steering_text, topic_id, pool,
):
    """No-op prep job: does nothing, leaves conversation in current state."""
    return None
