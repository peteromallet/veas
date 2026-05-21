"""Sprint 5 (T6) — Tests for the operator debug endpoint.

Covers:
(a) GET /api/live/ops/sessions/{session_id}/debug returns 200 with all sections.
(b) Returns 404 for unknown session.
(c) Returns 503 when conversations table missing.
(d) Conversation metadata includes canonicalized status.
(e) bot_turns keyed by conversation_id with kind/model/failure_reason/completion.
(f) transcript_turns under a separate key (SD3).
(g) Artifacts grouped by type with revision/current/deleted markers.
(h) Provenance links with durable write counts.
(i) Failure classes extracted from session_fields, bot_turns, non-chat metadata.
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
# FakePool that supports the debug endpoint queries
# --------------------------------------------------------------------------- #

_RELATIONSHIP_TOPIC_ID = UUID("00000000-0000-4000-8000-000000000001")


class _FakeTxn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, parent: "_DebugFakePool") -> None:
        self._parent = parent

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def execute(self, sql: str, *args: Any) -> str:
        return await self._parent.execute(sql, *args)


class _FakeAcquire:
    def __init__(self, parent: "_DebugFakePool") -> None:
        self._parent = parent

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._parent)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class DebugFakePool:
    """Fake pool for the operator debug endpoint tests.

    Handles SELECT * FROM mediator.conversations, bot_turns by
    conversation_id, transcript_turns, conversation_artifacts, and
    artifact_links.
    """

    def __init__(self) -> None:
        self._conversations: dict[UUID, dict[str, Any]] = {}
        self._bot_turns: dict[UUID, list[dict[str, Any]]] = {}
        self._transcript_turns: dict[UUID, list[dict[str, Any]]] = {}
        self._artifacts: dict[UUID, list[dict[str, Any]]] = {}
        self._artifact_links: list[dict[str, Any]] = []
        self._conversations_table_exists: bool = True

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        compact = " ".join(sql.split())
        if "information_schema.tables" in compact:
            return self._conversations_table_exists
        raise AssertionError(f"unexpected fetchval: {compact}")

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        compact = " ".join(sql.split())
        # SELECT * FROM mediator.conversations (used by debug + raw GET)
        if "SELECT * FROM mediator.conversations WHERE id" in compact:
            return self._conversations.get(args[0])  # type: ignore[arg-type]
        raise AssertionError(f"unexpected fetchrow: {compact}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        compact = " ".join(sql.split())

        if "FROM mediator.bot_turns" in compact:
            conv_id = args[0] if args else None
            return self._bot_turns.get(conv_id, [])  # type: ignore[arg-type]

        if "FROM mediator.transcript_turns" in compact:
            conv_id = args[0] if args else None
            return self._transcript_turns.get(conv_id, [])  # type: ignore[arg-type]

        if "FROM mediator.conversation_artifacts" in compact and "ORDER BY artifact_type" in compact:
            conv_id = args[0] if args else None
            return self._artifacts.get(conv_id, [])  # type: ignore[arg-type]

        if "FROM mediator.artifact_links" in compact:
            return self._artifact_links

        raise AssertionError(f"unexpected fetch: {compact}")

    async def execute(self, sql: str, *args: Any) -> str:
        return "OK"

    # ── Helpers for test setup ──────────────────────────────────────────

    def add_conversation(self, conv_id: UUID, **overrides: Any) -> None:
        row: dict[str, Any] = {
            "id": conv_id,
            "user_id": UUID("00000000-0000-0000-0000-000000000001"),
            "partner_user_id": None,
            "bot_id": "tante_rosi",
            "mode": "open",
            "status": "active",
            "steering_text": None,
            "prep_summary": "Here is the prep summary.",
            "current_item_id": None,
            "started_at": "2025-01-01T00:00:00Z",
            "ended_at": None,
            "created_at": "2025-01-01T00:00:00Z",
            "session_fields": {},
            "topic_id": _RELATIONSHIP_TOPIC_ID,
            "spend_usd_cents": 0,
        }
        row.update(overrides)
        self._conversations[conv_id] = row

    def add_bot_turn(
        self,
        conv_id: UUID,
        turn_id: UUID,
        *,
        kind: str | None = None,
        failure_reason: str | None = None,
        completed: bool = True,
        model_version: str = "anthropic/claude-sonnet-4-20250514",
        tool_call_count: int = 3,
        duration_ms: int = 5000,
    ) -> None:
        turn: dict[str, Any] = {
            "id": turn_id,
            "conversation_id": conv_id,
            "kind": kind,
            "model_version": model_version,
            "failure_reason": failure_reason,
            "completed_at": "2025-01-01T00:01:00Z" if completed else None,
            "started_at": "2025-01-01T00:00:55Z",
            "tool_call_count": tool_call_count,
            "duration_ms": duration_ms,
        }
        self._bot_turns.setdefault(conv_id, []).append(turn)

    def add_transcript_turn(
        self,
        conv_id: UUID,
        turn_id: UUID,
        *,
        speaker_label: str = "speaker_0",
        speaker_role: str = "primary",
        text: str = "Hello.",
    ) -> None:
        tt: dict[str, Any] = {
            "id": turn_id,
            "conversation_id": conv_id,
            "speaker_label": speaker_label,
            "speaker_role": speaker_role,
            "text": text,
            "ts": "2025-01-01T00:00:30Z",
            "asr_confidence": 0.95,
            "active_item_id": None,
            "was_routing_input": False,
        }
        self._transcript_turns.setdefault(conv_id, []).append(tt)

    def add_artifact(
        self,
        conv_id: UUID,
        artifact_id: UUID,
        *,
        artifact_type: str = "live_prep_brief",
        revision_number: int = 1,
        payload: dict[str, Any] | None = None,
        created_by_turn_id: UUID | None = None,
        deleted: bool = False,
    ) -> None:
        art: dict[str, Any] = {
            "id": artifact_id,
            "conversation_id": conv_id,
            "artifact_type": artifact_type,
            "revision_number": revision_number,
            "payload": payload or {},
            "payload_version": 1,
            "created_by_turn_id": created_by_turn_id,
            "deleted_at": "2025-06-01T00:00:00Z" if deleted else None,
            "created_at": "2025-01-01T00:02:00Z",
            "expires_at": None,
        }
        self._artifacts.setdefault(conv_id, []).append(art)

    def add_artifact_link(
        self,
        link_id: UUID,
        *,
        artifact_id: UUID,
        target_table: str = "memories",
        target_id: UUID | None = None,
        relation: str = "extracted_memory",
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self._artifact_links.append({
            "id": link_id,
            "artifact_id": artifact_id,
            "target_table": target_table,
            "target_id": target_id or uuid4(),
            "relation": relation,
            "evidence": evidence,
            "deleted_at": None,
            "created_at": "2025-01-01T00:03:00Z",
        })


# --------------------------------------------------------------------------- #
# FastAPI test app factory
# --------------------------------------------------------------------------- #


def _make_app(monkeypatch, pool: DebugFakePool) -> FastAPI:
    monkeypatch.setenv("STAGING", "1")
    import app.bots.registry as _bot_reg
    monkeypatch.setattr(_bot_reg, "_STAGING_BOTS_REGISTERED", False)

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


def _client(monkeypatch, pool: DebugFakePool) -> TestClient:
    return TestClient(_make_app(monkeypatch, pool))


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class TestDebugEndpointBasic:
    """Basic behaviour: 404, 503, 200 with all sections."""

    def test_returns_404_for_unknown_session(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        client = _client(monkeypatch, pool)
        resp = client.get(
            f"/api/live/ops/sessions/{uuid4()}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 404

    def test_returns_503_when_conversations_table_missing(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        pool._conversations_table_exists = False
        client = _client(monkeypatch, pool)
        resp = client.get(
            f"/api/live/ops/sessions/{uuid4()}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 503

    def test_returns_all_sections_for_known_session(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        pool.add_conversation(session_id, status="active")
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == str(session_id)
        assert "conversation" in data
        assert "bot_turns" in data
        assert "transcript_turns" in data
        assert "artifacts" in data
        assert "provenance" in data
        assert "failure_classes" in data


class TestDebugConversationMetadata:
    """Conversation metadata includes id, status (canonicalized), bot_id,
    user_id, mode, timestamps, session_fields."""

    def test_status_is_canonicalized(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        # Insert with legacy 'live' status — should canonicalize to 'active'.
        pool.add_conversation(session_id, status="live")
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["conversation"]["status"] == "active"

    def test_includes_all_metadata_fields(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        pool.add_conversation(
            session_id,
            status="active",
            bot_id="tante_rosi",
            mode="steered",
            steering_text="Focus on this.",
            prep_summary="Summary text.",
        )
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        conv = resp.json()["conversation"]
        assert conv["id"] == str(session_id)
        assert conv["status"] == "active"
        assert conv["bot_id"] == "tante_rosi"
        assert conv["mode"] == "steered"
        assert conv["steering_text"] == "Focus on this."
        assert conv["prep_summary"] == "Summary text."
        assert conv["session_fields"] == {}
        assert "started_at" in conv
        assert "created_at" in conv


class TestDebugBotTurns:
    """bot_turns found by conversation_id include kind, turn id, model,
    provider, failure_reason, completion state, tool_call_count."""

    def test_bot_turns_by_conversation_id(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        turn_id = uuid4()
        pool.add_conversation(session_id)
        pool.add_bot_turn(
            session_id, turn_id,
            kind="live_prep",
            model_version="deepseek/deepseek-chat",
            tool_call_count=5,
        )
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        bot_turns = resp.json()["bot_turns"]
        assert len(bot_turns) == 1
        bt = bot_turns[0]
        assert bt["id"] == str(turn_id)
        assert bt["kind"] == "live_prep"
        assert bt["turn_id"] == str(turn_id)
        assert bt["model"] == "deepseek/deepseek-chat"
        assert bt["provider"] == "deepseek"
        assert bt["completed"] is True
        assert bt["tool_call_count"] == 5

    def test_bot_turns_with_failure_reason(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        turn_id = uuid4()
        pool.add_conversation(session_id)
        pool.add_bot_turn(
            session_id, turn_id,
            kind="live_debrief",
            failure_reason="submit_missing",
            completed=False,
        )
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        bt = resp.json()["bot_turns"][0]
        assert bt["failure_reason"] == "submit_missing"
        assert bt["completed"] is False
        assert bt["completed_at"] is None

    def test_provider_extracted_from_model_version(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        pool.add_conversation(session_id)
        pool.add_bot_turn(session_id, uuid4(), model_version="openai/gpt-4o")
        pool.add_bot_turn(session_id, uuid4(), model_version="groq/llama3-70b")
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        bts = resp.json()["bot_turns"]
        providers = {bt["provider"] for bt in bts}
        assert "openai" in providers
        assert "groq" in providers


class TestDebugTranscriptTurns:
    """transcript_turns returned under separate key (SD3)."""

    def test_transcript_turns_separate_key(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        pool.add_conversation(session_id)
        tid1 = uuid4()
        tid2 = uuid4()
        pool.add_transcript_turn(session_id, tid1, text="Hi there.")
        pool.add_transcript_turn(
            session_id, tid2, speaker_role="bot", text="Hello!"
        )
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "transcript_turns" in data
        # transcript_turns is separate from bot_turns
        assert isinstance(data["transcript_turns"], list)
        assert isinstance(data["bot_turns"], list)
        # Both should be present
        tt = data["transcript_turns"]
        assert len(tt) == 2
        assert tt[0]["text"] == "Hi there."
        assert tt[0]["speaker_role"] == "primary"
        assert tt[1]["text"] == "Hello!"
        assert tt[1]["speaker_role"] == "bot"


class TestDebugArtifacts:
    """Artifacts grouped by type with revision/current/deleted markers."""

    def test_artifacts_grouped_by_type(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        pool.add_conversation(session_id)
        aid1 = uuid4()
        aid2 = uuid4()
        pool.add_artifact(session_id, aid1, artifact_type="live_prep_brief", revision_number=1)
        pool.add_artifact(session_id, aid2, artifact_type="live_debrief", revision_number=1)
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        arts = resp.json()["artifacts"]
        assert "live_prep_brief" in arts
        assert "live_debrief" in arts
        assert len(arts["live_prep_brief"]) == 1
        assert len(arts["live_debrief"]) == 1

    def test_current_marker_on_highest_non_deleted_revision(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        pool.add_conversation(session_id)
        aid1 = uuid4()
        aid2 = uuid4()
        pool.add_artifact(session_id, aid1, artifact_type="live_prep_brief", revision_number=1)
        pool.add_artifact(session_id, aid2, artifact_type="live_prep_brief", revision_number=2)
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        arts = resp.json()["artifacts"]["live_prep_brief"]
        # Two entries, newest revision first (ORDER BY ... DESC)
        assert len(arts) == 2
        rev1 = [a for a in arts if a["revision_number"] == 1][0]
        rev2 = [a for a in arts if a["revision_number"] == 2][0]
        assert rev1["current"] is False
        assert rev2["current"] is True

    def test_deleted_marker(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        pool.add_conversation(session_id)
        aid = uuid4()
        pool.add_artifact(session_id, aid, deleted=True)
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        art = resp.json()["artifacts"]["live_prep_brief"][0]
        assert art["deleted"] is True
        assert art["current"] is False

    def test_artifact_links_included(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        pool.add_conversation(session_id)
        aid = uuid4()
        lid = uuid4()
        pool.add_artifact(session_id, aid)
        pool.add_artifact_link(lid, artifact_id=aid)
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        art = resp.json()["artifacts"]["live_prep_brief"][0]
        assert len(art["links"]) == 1
        assert art["links"][0]["id"] == str(lid)


class TestDebugProvenance:
    """Provenance links with target table/id/relation/evidence plus
    aggregate durable write counts."""

    def test_provenance_links_present(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        pool.add_conversation(session_id)
        aid = uuid4()
        lid = uuid4()
        pool.add_artifact(session_id, aid)
        pool.add_artifact_link(
            lid, artifact_id=aid,
            target_table="memories",
            relation="extracted_memory",
            evidence={"quote": "some evidence"},
        )
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        prov = resp.json()["provenance"]
        assert len(prov["links"]) == 1
        link = prov["links"][0]
        assert link["target_table"] == "memories"
        assert link["relation"] == "extracted_memory"
        assert link["evidence"] == {"quote": "some evidence"}

    def test_durable_write_counts_aggregated(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        pool.add_conversation(session_id)
        aid = uuid4()
        pool.add_artifact(session_id, aid)
        pool.add_artifact_link(uuid4(), artifact_id=aid, target_table="memories", relation="extracted_memory")
        pool.add_artifact_link(uuid4(), artifact_id=aid, target_table="observations", relation="extracted_observation")
        pool.add_artifact_link(uuid4(), artifact_id=aid, target_table="memories", relation="extracted_memory")
        # conversation-scoped tables should not be counted
        pool.add_artifact_link(uuid4(), artifact_id=aid, target_table="conversation_items", relation="planned_item")
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        counts = resp.json()["provenance"]["durable_write_counts"]
        assert counts.get("memories") == 2
        assert counts.get("observations") == 1
        # conversation_items excluded from durable counts
        assert "conversation_items" not in counts


class TestDebugFailureClasses:
    """Failure classes extracted from session_fields, bot_turns,
    and non-chat result metadata."""

    def test_failure_from_session_fields(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        pool.add_conversation(
            session_id,
            session_fields={"prep_error": "prep crashed", "debrief_failure_reason": "timeout"},
        )
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        fc = resp.json()["failure_classes"]
        assert fc["session"]["prep_error"] == "prep crashed"
        assert fc["session"]["debrief_error"] == "timeout"

    def test_failure_from_bot_turns(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        turn_id = uuid4()
        pool.add_conversation(session_id)
        pool.add_bot_turn(
            session_id, turn_id,
            kind="live_prep",
            failure_reason="live_prep_submit_missing",
        )
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        fc = resp.json()["failure_classes"]
        assert len(fc["bot_turns"]) == 1
        assert fc["bot_turns"][0]["turn_id"] == str(turn_id)
        assert fc["bot_turns"][0]["failure_reason"] == "live_prep_submit_missing"
        assert fc["bot_turns"][0]["kind"] == "live_prep"
        assert "failure_class" in fc["bot_turns"][0]

    def test_non_chat_metadata(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        turn_id = uuid4()
        pool.add_conversation(session_id)
        pool.add_bot_turn(session_id, turn_id, kind="live_prep")
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        fc = resp.json()["failure_classes"]
        assert len(fc["non_chat"]) == 1
        assert fc["non_chat"][0]["turn_id"] == str(turn_id)
        assert fc["non_chat"][0]["kind"] == "live_prep"
        assert fc["non_chat"][0]["outcome"] == "success"

    def test_non_chat_failure(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        turn_id = uuid4()
        pool.add_conversation(session_id)
        pool.add_bot_turn(
            session_id, turn_id,
            kind="live_debrief",
            failure_reason="bounded_loop_exceeded",
        )
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        fc = resp.json()["failure_classes"]
        assert len(fc["non_chat"]) == 1
        nc = fc["non_chat"][0]
        assert nc["failure_reason"] == "bounded_loop_exceeded"
        assert "failure_class" in nc
        assert "outcome" not in nc  # failure entries don't have outcome

    def test_legacy_status_canonicalized(self, monkeypatch: Any) -> None:
        """The debug endpoint must canonicalize historical status values."""
        pool = DebugFakePool()
        session_id = uuid4()
        pool.add_conversation(session_id, status="synthesizing")  # legacy
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json()["conversation"]["status"] == "debriefing"


class TestDebugEmptySession:
    """Debug endpoint on a session with no bot turns, transcripts, or artifacts."""

    def test_empty_session_returns_empty_collections(self, monkeypatch: Any) -> None:
        pool = DebugFakePool()
        session_id = uuid4()
        pool.add_conversation(session_id)
        client = _client(monkeypatch, pool)

        resp = client.get(
            f"/api/live/ops/sessions/{session_id}/debug",
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["bot_turns"] == []
        assert data["transcript_turns"] == []
        assert data["artifacts"] == {}
        assert data["provenance"]["links"] == []
        assert data["provenance"]["durable_write_counts"] == {}
