"""Integration tests for create_conversation_plan (T9, part 1).

Covers:
(a) mode='open' (no prep_summary) → row with status='ready', mode='open'
(b) mode='steered' (with prep_summary) → row with status='ready', mode='steered'
(c) topic_id matches primary_topic_id_for result
(d) conversation_items rows mirror agenda order
(e) current_item_id set after items are inserted (FK ordering survives)
(f) GET /api/live/sessions as user → conversation appears (Sprint 1 discovery)
(g) GET /sessions/{id}/card → status='ready', items > 0
(h) Topic-resolution failure path → ToolCallRejected, no row persisted
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.bots.registry import primary_topic_id_for
from app.config import get_settings
from app.models.user import User
from app.routers.live_voice import router as live_voice_router
from app.services.auth import jwt as live_jwt
from app.services.tools.registry import call_tool
from app.services.tools.write_tools import ToolCallRejected
from app.services.turn_context import TurnContext

from tests.test_live_router_prep import LiveVoiceFakePool, _fake_primary_topic_id_for

_RELATIONSHIP_TOPIC_ID = UUID("00000000-0000-4000-8000-000000000001")

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


# ── Extended FakePool ────────────────────────────────────────────────────────

class _PlanFakeConn:
    """Connection proxy that delegates to the pool, supporting fetchrow/fetch."""

    def __init__(self, parent: "PlanCreateFakePool") -> None:
        self._parent = parent

    def transaction(self):
        from tests.test_live_router_prep import _FakeTxn
        return _FakeTxn()

    async def execute(self, sql: str, *args: Any) -> str:
        return await self._parent.execute(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return await self._parent.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return await self._parent.fetch(sql, *args)


class _PlanFakeAcquire:
    def __init__(self, parent: "PlanCreateFakePool") -> None:
        self._parent = parent

    async def __aenter__(self) -> _PlanFakeConn:
        return _PlanFakeConn(self._parent)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class PlanCreateFakePool(LiveVoiceFakePool):
    """LiveVoiceFakePool extended with plan-tool create SQL patterns.

    Handles:
    - INSERT INTO mediator.conversations ... RETURNING id (via fetchrow)
    - Plan-specific INSERT INTO mediator.conversation_items column order
    - DELETE FROM mediator.conversation_items (for update reconciliation)
    - SELECT id, status, mode, current_item_id FROM mediator.conversations
    - SELECT id, status, mode FROM mediator.conversations ... FOR UPDATE
    - INSERT INTO tool_calls (via execute, handled by fallback "OK")
    """

    def acquire(self):
        return _PlanFakeAcquire(self)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        compact = " ".join(sql.split())

        # INSERT INTO mediator.conversations ... RETURNING id (plan tools)
        if compact.startswith("INSERT INTO mediator.conversations (user_id, bot_id, topic_id, status"):
            row_id = uuid4()
            row: dict[str, Any] = {
                "id": row_id,
                "status": "ready",
                "mode": "open",
                "prep_summary": None,
                "current_item_id": None,
                "session_fields": {},
                "steering_text": None,
            }
            # args: user_id, bot_id, topic_id, mode, prep_summary
            if len(args) >= 1:
                row["user_id"] = args[0]
            if len(args) >= 2:
                row["bot_id"] = args[1]
            if len(args) >= 3:
                row["topic_id"] = args[2]
            if len(args) >= 4:
                row["mode"] = args[3]
            if len(args) >= 5 and args[4] is not None:
                row["prep_summary"] = args[4]
            self._conversations[row_id] = row
            return {"id": row_id}

        # SELECT id, status, mode, current_item_id FROM mediator.conversations
        # WHERE id=$1 AND user_id=$2 (read_conversation_plan)
        if compact.startswith("SELECT id, status, mode, current_item_id FROM mediator.conversations WHERE id"):
            conv_id = self._resolve_uuid(args[0]) if args else None
            conv = self._conversations.get(conv_id) if conv_id is not None else None
            if conv is None:
                return None
            # Check ownership: user_id must match
            if len(args) >= 2 and conv.get("user_id") != args[1]:
                return None
            return {
                "id": conv["id"],
                "status": conv.get("status", "ready"),
                "mode": conv.get("mode", "open"),
                "current_item_id": conv.get("current_item_id"),
            }

        # SELECT id, status, mode FROM mediator.conversations
        # WHERE id=$1 AND user_id=$2 FOR UPDATE (update_conversation_plan)
        if compact.startswith("SELECT id, status, mode FROM mediator.conversations WHERE id"):
            conv_id = self._resolve_uuid(args[0]) if args else None
            conv = self._conversations.get(conv_id) if conv_id is not None else None
            if conv is None:
                return None
            if len(args) >= 2 and conv.get("user_id") != args[1]:
                return None
            return {
                "id": conv["id"],
                "status": conv.get("status", "ready"),
                "mode": conv.get("mode", "open"),
            }

        # SELECT id, user_id, bot_id, mode, status, prep_summary,
        # current_item_id, started_at, session_fields FROM mediator.conversations
        # WHERE id = $1 (get_session_card)
        if compact.startswith("SELECT id, user_id, bot_id, mode, status, prep_summary, current_item_id, started_at, session_fields"):
            conv_id = self._resolve_uuid(args[0]) if args else None
            conv = self._conversations.get(conv_id) if conv_id is not None else None
            if conv is None:
                return None
            return {
                "id": conv["id"],
                "user_id": conv.get("user_id"),
                "bot_id": conv.get("bot_id", "unknown"),
                "mode": conv.get("mode", "open"),
                "status": conv.get("status", "ready"),
                "prep_summary": conv.get("prep_summary"),
                "current_item_id": conv.get("current_item_id"),
                "started_at": conv.get("started_at"),
                "session_fields": conv.get("session_fields", {}),
            }

        # Fall back to parent for all other patterns
        try:
            return await super().fetchrow(sql, *args)
        except AssertionError:
            return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        compact = " ".join(sql.split())

        # SELECT id, title, priority, order_hint FROM mediator.conversation_items
        # WHERE conversation_id=$1 AND kind='planned' ORDER BY order_hint
        if compact.startswith("SELECT id, title, priority, order_hint FROM mediator.conversation_items"):
            conv_id = self._resolve_uuid(args[0]) if args else None
            items = self._items.get(conv_id, [])
            # Filter to kind='planned' and sort by order_hint
            planned = sorted(
                [i for i in items if i.get("kind") == "planned"],
                key=lambda i: i.get("order_hint", 0),
            )
            return [
                {
                    "id": i["id"],
                    "title": i.get("title", ""),
                    "priority": i.get("priority", "should"),
                    "order_hint": i.get("order_hint", 0),
                }
                for i in planned
            ]

        # SELECT c.id, c.status, c.started_at, c.created_at, (subquery) ...
        # FROM mediator.conversations c WHERE c.user_id=$1 AND c.status IN (...)
        # ORDER BY c.started_at DESC LIMIT $2 (list_conversation_plans)
        if "FROM mediator.conversations c WHERE c.user_id" in compact:
            user_id = self._resolve_uuid(args[0]) if args else None
            results = []
            for conv in self._conversations.values():
                uid = conv.get("user_id")
                if uid != user_id:
                    continue
                status = conv.get("status", "")
                if status not in ("prepping", "preparing", "ready"):
                    continue
                items = self._items.get(conv["id"], [])
                planned_items = sorted(
                    [i for i in items if i.get("kind") == "planned"],
                    key=lambda i: i.get("order_hint", 0),
                )
                first_title = planned_items[0]["title"] if planned_items else None
                from datetime import datetime, timezone as _tz
                results.append({
                    "id": conv["id"],
                    "status": status,
                    "started_at": conv.get("started_at"),
                    "created_at": conv.get("created_at") or datetime(2025, 1, 1, tzinfo=_tz.utc),
                    "first_title": first_title,
                    "item_count": len(planned_items),
                })
            # Sort by started_at DESC
            results.sort(
                key=lambda r: (r["started_at"] or ""),
                reverse=True,
            )
            limit = args[1] if len(args) > 1 else 5
            return results[:limit]

        # Fall back to parent
        try:
            return await super().fetch(sql, *args)
        except AssertionError:
            return []

    async def execute(self, sql: str, *args: Any) -> str:
        compact = " ".join(sql.split())

        # Plan-specific INSERT INTO mediator.conversation_items
        # Column order: (id, conversation_id, theme_id, kind, title, intent, ask,
        #                done_when, next_item_ids, priority, speaker_scope,
        #                coverage_evidence_required, order_hint)
        # NOTE: 'kind' is a literal 'planned' in the VALUES, NOT a parameter.
        # So args positions: 0=id, 1=conv_id, 2=theme_id, 3=title, 4=intent,
        #                    5=ask, 6=done_when, 7=next_item_ids, 8=priority,
        #                    9=speaker_scope, 10=coverage_evidence_required, 11=order_hint
        if compact.startswith("INSERT INTO mediator.conversation_items (id, conversation_id, theme_id"):
            conv_id = self._resolve_uuid(args[1]) if len(args) > 1 else None
            item_id = args[0] if args else uuid4()
            if conv_id is not None:
                self._items.setdefault(conv_id, []).append({
                    "id": item_id,
                    "conversation_id": conv_id,
                    "theme_id": args[2] if len(args) > 2 else None,
                    "kind": "planned",
                    "title": args[3] if len(args) > 3 else "",
                    "intent": args[4] if len(args) > 4 else None,
                    "ask": args[5] if len(args) > 5 else None,
                    "done_when": args[6] if len(args) > 6 else None,
                    "next_item_ids": args[7] if len(args) > 7 else [],
                    "priority": args[8] if len(args) > 8 else "should",
                    "speaker_scope": args[9] if len(args) > 9 else "any",
                    "coverage_evidence_required": args[10] if len(args) > 10 else False,
                    "order_hint": args[11] if len(args) > 11 else 0,
                })
            return "INSERT 0 1"

        # DELETE FROM mediator.conversation_items WHERE conversation_id=$1 AND kind='planned'
        if compact.startswith("DELETE FROM mediator.conversation_items WHERE conversation_id"):
            conv_id = self._resolve_uuid(args[0]) if args else None
            if conv_id is not None and conv_id in self._items:
                self._items[conv_id] = [
                    i for i in self._items[conv_id]
                    if i.get("kind") != "planned"
                ]
            return "DELETE 1"

        # UPDATE mediator.conversations SET current_item_id=$1 WHERE id=$2
        # (plan tools: $1 = new item UUID, $2 = conversation UUID)
        if compact.startswith("UPDATE mediator.conversations SET current_item_id=$1 WHERE id=$2"):
            item_uuid = args[0] if len(args) > 0 else None
            conv_id = self._resolve_uuid(args[1]) if len(args) > 1 else None
            if conv_id is not None and conv_id in self._conversations:
                self._conversations[conv_id]["current_item_id"] = item_uuid
            return "UPDATE 1"

        # UPDATE mediator.conversations SET current_item_id=$1, prep_summary=$2, mode=$3 WHERE id=$4
        if "current_item_id" in compact and "prep_summary" in compact and "mode" in compact:
            conv_id = self._resolve_uuid(args[3]) if len(args) > 3 else None
            if conv_id is not None and conv_id in self._conversations:
                self._conversations[conv_id]["current_item_id"] = args[0] if len(args) > 0 else None
                self._conversations[conv_id]["prep_summary"] = args[1] if len(args) > 1 else None
                self._conversations[conv_id]["mode"] = args[2] if len(args) > 2 else "open"
            return "UPDATE 1"

        # Fall back to parent
        return await super().execute(sql, *args)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _prime_env(monkeypatch) -> None:
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("STAGING", "1")
    get_settings.cache_clear()


def _token_for(user_id: UUID) -> str:
    return live_jwt.mint(user_id=str(user_id))


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_app(monkeypatch, pool: PlanCreateFakePool) -> FastAPI:
    import app.bots.registry as _bot_reg

    monkeypatch.setenv("STAGING", "1")
    monkeypatch.setattr(_bot_reg, "_STAGING_BOTS_REGISTERED", False)
    for k, v in _REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "true")
    get_settings.cache_clear()
    _bot_reg._maybe_register_staging_bots()

    app = FastAPI()
    app.state.pool = pool
    app.include_router(live_voice_router)
    return app


def _client(monkeypatch, pool: PlanCreateFakePool) -> TestClient:
    return TestClient(_make_app(monkeypatch, pool))


def _make_turn_ctx(
    pool: PlanCreateFakePool,
    *,
    user_id: UUID | None = None,
    bot_id: str = "mediator",
    current_step: str = "respond",
) -> TurnContext:
    uid = user_id or uuid4()
    user = User(id=uid, name="TestUser", phone="15555550100", timezone="UTC")
    return TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id=bot_id,
        current_step=current_step,
    )


# ── Tests ────────────────────────────────────────────────────────────────────


class TestCreateConversationPlan:
    """Integration tests for create_conversation_plan."""

    @pytest.mark.anyio
    async def test_create_open_mode_no_prep_summary(self, monkeypatch) -> None:
        """Mode='open' when prep_summary is None/empty."""
        _prime_env(monkeypatch)
        pool = PlanCreateFakePool()
        monkeypatch.setattr(
            "app.bots.registry.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        ctx = _make_turn_ctx(pool, current_step="respond")

        result = await call_tool(
            "create_conversation_plan",
            {
                "plan_markdown": "1. First item\n2. Second item",
                "prep_summary": None,
            },
            ctx,
        )

        assert result["status"] == "ready"
        assert "conversation_id" in result
        conv_id = result["conversation_id"]
        assert isinstance(conv_id, str)

        # Assert row in pool
        conv_uuid = UUID(conv_id)
        assert conv_uuid in pool._conversations
        conv = pool._conversations[conv_uuid]
        assert conv["status"] == "ready"
        assert conv["mode"] == "open"
        assert conv["prep_summary"] is None

        # Assert topic_id matches
        assert conv["topic_id"] == _RELATIONSHIP_TOPIC_ID

        # Assert items in pool
        items = pool._items.get(conv_uuid, [])
        assert len(items) == 2
        assert items[0]["title"] == "First item"
        assert items[1]["title"] == "Second item"
        assert items[0]["kind"] == "planned"
        assert items[1]["kind"] == "planned"

        # Assert current_item_id set (FK ordering)
        assert conv["current_item_id"] is not None
        assert conv["current_item_id"] == items[0]["id"]

    @pytest.mark.anyio
    async def test_create_steered_mode_with_prep_summary(self, monkeypatch) -> None:
        """Mode='steered' when prep_summary is provided."""
        _prime_env(monkeypatch)
        pool = PlanCreateFakePool()
        monkeypatch.setattr(
            "app.bots.registry.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        ctx = _make_turn_ctx(pool, current_step="respond")

        result = await call_tool(
            "create_conversation_plan",
            {
                "plan_markdown": "1. Agenda item A\n2. Agenda item B\n3. Agenda item C",
                "prep_summary": "Steering context for this conversation",
            },
            ctx,
        )

        assert result["status"] == "ready"
        conv_id = UUID(result["conversation_id"])
        conv = pool._conversations[conv_id]
        assert conv["status"] == "ready"
        assert conv["mode"] == "steered"
        assert conv["prep_summary"] == "Steering context for this conversation"

        items = pool._items.get(conv_id, [])
        assert len(items) == 3
        assert items[0]["priority"] == "must"  # first item promoted
        assert items[1]["priority"] == "should"
        assert items[2]["priority"] == "should"

    @pytest.mark.anyio
    async def test_items_mirror_agenda_order(self, monkeypatch) -> None:
        """conversation_items rows reflect agenda ordering."""
        _prime_env(monkeypatch)
        pool = PlanCreateFakePool()
        monkeypatch.setattr(
            "app.bots.registry.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        ctx = _make_turn_ctx(pool, current_step="respond")

        result = await call_tool(
            "create_conversation_plan",
            {
                "plan_markdown": "1. Alpha\n2. Beta\n3. Gamma\n4. Delta\n5. Epsilon",
                "prep_summary": None,
            },
            ctx,
        )

        conv_id = UUID(result["conversation_id"])
        items = pool._items.get(conv_id, [])
        assert len(items) == 5

        # Items should be in order_hint order
        sorted_items = sorted(items, key=lambda i: i.get("order_hint", 0))
        assert [i["title"] for i in sorted_items] == [
            "Alpha", "Beta", "Gamma", "Delta", "Epsilon",
        ]

    @pytest.mark.anyio
    async def test_current_item_id_set_after_items(self, monkeypatch) -> None:
        """current_item_id is set AFTER items are inserted (FK ordering)."""
        _prime_env(monkeypatch)
        pool = PlanCreateFakePool()
        monkeypatch.setattr(
            "app.bots.registry.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        ctx = _make_turn_ctx(pool, current_step="respond")

        result = await call_tool(
            "create_conversation_plan",
            {
                "plan_markdown": "1. Item one\n2. Item two",
                "prep_summary": None,
            },
            ctx,
        )

        conv_id = UUID(result["conversation_id"])
        conv = pool._conversations[conv_id]
        items = pool._items.get(conv_id, [])
        first_item = sorted(items, key=lambda i: i.get("order_hint", 0))[0]

        assert conv["current_item_id"] == first_item["id"]

    @pytest.mark.anyio
    async def test_topic_resolution_failure_rollback(self, monkeypatch) -> None:
        """When primary_topic_id_for raises, ToolCallRejected is raised and
        no row is persisted."""
        _prime_env(monkeypatch)
        pool = PlanCreateFakePool()

        async def _failing_topic_id(pool, bot_spec):
            raise ValueError("no topic found")

        monkeypatch.setattr(
            "app.bots.registry.primary_topic_id_for",
            _failing_topic_id,
        )
        ctx = _make_turn_ctx(pool, current_step="respond")

        # call_tool catches ToolCallRejected and returns an error dict
        result = await call_tool(
            "create_conversation_plan",
            {
                "plan_markdown": "1. Test item",
                "prep_summary": None,
            },
            ctx,
        )

        assert result.get("is_error") is True
        assert "topic resolution failed" in result.get("error", "")
        # No conversation row persisted
        assert len(pool._conversations) == 0

    @pytest.mark.anyio
    async def test_discovery_via_list_sessions(self, monkeypatch) -> None:
        """GET /api/live/sessions returns the created conversation."""
        _prime_env(monkeypatch)
        pool = PlanCreateFakePool()
        monkeypatch.setattr(
            "app.bots.registry.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        ctx = _make_turn_ctx(pool, current_step="respond")
        user_id = ctx.user.id

        result = await call_tool(
            "create_conversation_plan",
            {
                "plan_markdown": "1. Discovery test item",
                "prep_summary": None,
            },
            ctx,
        )
        conv_id = result["conversation_id"]

        # Now query via HTTP
        client = _client(monkeypatch, pool)
        token = _token_for(user_id)
        resp = client.get(
            "/api/live/sessions",
            headers=_auth_header(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        # Response may be a list or a dict with 'sessions' key
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        assert isinstance(sessions, list)
        # Find our conversation in the list
        conv_ids = [item["id"] for item in sessions]
        assert conv_id in conv_ids

    @pytest.mark.anyio
    async def test_card_endpoint_returns_ready_with_items(self, monkeypatch) -> None:
        """GET /sessions/{id}/card returns status='ready' and items > 0."""
        _prime_env(monkeypatch)
        pool = PlanCreateFakePool()
        monkeypatch.setattr(
            "app.bots.registry.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        ctx = _make_turn_ctx(pool, current_step="respond")
        user_id = ctx.user.id

        result = await call_tool(
            "create_conversation_plan",
            {
                "plan_markdown": "1. Card item one\n2. Card item two\n3. Card item three",
                "prep_summary": "Steering context",
            },
            ctx,
        )
        conv_id = result["conversation_id"]

        client = _client(monkeypatch, pool)
        token = _token_for(user_id)
        resp = client.get(
            f"/api/live/sessions/{conv_id}/card",
            headers=_auth_header(token),
        )
        assert resp.status_code == 200
        data = resp.json()

        # The card endpoint canonicalizes status; 'ready' stays 'ready'
        assert data["status"] == "ready"
        assert len(data["items"]) == 3
        assert data["mode"] == "steered"

    @pytest.mark.anyio
    async def test_create_in_read_step_is_rejected(self, monkeypatch) -> None:
        """create_conversation_plan is rejected in the read step."""
        _prime_env(monkeypatch)
        pool = PlanCreateFakePool()
        monkeypatch.setattr(
            "app.bots.registry.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        ctx = _make_turn_ctx(pool, current_step="read")

        result = await call_tool(
            "create_conversation_plan",
            {
                "plan_markdown": "1. Test",
                "prep_summary": None,
            },
            ctx,
        )

        assert result.get("is_error") is True
        assert "not allowed" in result.get("error", "")
