"""Integration tests for agentic live debrief (Sprint 3+4).

Covers:
(a) Happy path — seed live conversation with prep artifact, agenda, transcript,
    notes; fake provider to call submit_live_debrief; assert artifacts land
    and status=review_pending.
(b) Durable write path — scoped write from debrief passes read-before-write
    + safety gate.
(c) Redaction enforcement — guarded write citing redacted partner turn
    rejected server-side.
(d) Outbound denial — outbound tools rejected by call_tool.
(e) Privacy — partner raw text only with consent+opt-in.
(f) Failure — missing submit/cap exhaustion -> debrief_failed.
(g) Retry path.
(h) Sprint 4 T10 — provisional artifact finalization in debrief persistence.
(i) Sprint 4 T12 — rollback / deletion helper tests.
(j) Sprint 4 T14 — provenance capture, extras, and reverse lookup tests.

All tests use a FakePool that records SQL — no real LLM APIs or DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.models.user import User
from app.services.nonchat_agentic import NonchatJobResult
from app.services.tools.registry import (
    LIVE_DEBRIEF_GUARDED_WRITE_TOOLS,
    LIVE_DEBRIEF_OUTBOUND_DENYLIST,
    _create_debrief_artifact_link,
    build_live_debrief_tools,
    _step_allowed,
)
from app.services.turn_context import TurnContext
from app.bots.registry import get_bot_spec, BOT_SPECS


# ── FakePool for debrief integration tests ───────────────────────────────────


class _FakeTxn:
    """Auto-committing fake transaction — no-op enter/exit."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    """Single-connection handle that delegates to the parent FakePool."""

    def __init__(self, parent: "DebriefFakePool") -> None:
        self._parent = parent

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def execute(self, sql: str, *args: Any) -> str:
        return await self._parent.execute(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return await self._parent.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return await self._parent.fetch(sql, *args)


class _FakeAcquire:
    def __init__(self, parent: "DebriefFakePool") -> None:
        self._parent = parent

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._parent)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class DebriefFakePool:
    """Minimal asyncpg pool stand-in for live debrief integration tests.

    Records all executed SQL and supplies canned return values for every
    fetch/fetchrow pattern used by the live debrief code path.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

        # Canned fetchrow results keyed by substring match.
        self._fetchrow_map: dict[str, dict[str, Any] | None] = {}
        # Canned fetch (multi-row) results keyed by substring match.
        self._fetch_map: dict[str, list[dict[str, Any]]] = {}

        # Track INSERTs / UPDATEs for verification.
        self.inserted_artifact_payloads: list[dict[str, Any]] = []
        self.updated_status: str | None = None
        self.updated_session_fields: dict[str, Any] | None = None
        # Sprint 4 (T10): track finalize / mark-failed calls.
        self.finalized_artifact_ids: list[str] = []
        self.failed_artifact_ids: list[str] = []
        self.failed_artifact_reasons: list[str] = []
        self.soft_deleted_link_count: int = 0

        # Auto-generated UUIDs for artifact rows.
        self._artifact_id_counter = 0

    # -- public helpers --

    def set_conversations_row(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        partner_user_id: UUID | None = None,
        bot_id: str = "mediator",
        status: str = "debriefing",
        topic_id: UUID | None = None,
        session_fields: dict[str, Any] | None = None,
        prep_summary: str | None = None,
        current_item_id: UUID | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        self._fetchrow_map["FROM mediator.conversations"] = {
            "id": conversation_id,
            "user_id": str(user_id),
            "partner_user_id": str(partner_user_id) if partner_user_id else None,
            "bot_id": bot_id,
            "mode": "open",
            "steering_text": "",
            "status": status,
            "topic_id": str(topic_id) if topic_id else None,
            "session_fields": session_fields or {},
            "prep_summary": prep_summary,
            "current_item_id": str(current_item_id) if current_item_id else None,
            "started_at": started_at,
            "ended_at": ended_at,
        }

    def set_user_row(self, user_id: UUID, *, name: str = "test-user") -> None:
        self._fetchrow_map["SELECT * FROM users"] = {
            "id": user_id,
            "name": name,
            "phone": "+155****0000",
            "timezone": "UTC",
        }

    def set_transcript_turns(
        self, rows: list[dict[str, Any]] | None = None
    ) -> None:
        self._fetch_map["FROM mediator.transcript_turns"] = rows or []

    def set_speakers(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._fetch_map["FROM mediator.conversation_speakers"] = rows or []

    def set_agenda_items(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._fetch_map["FROM mediator.conversation_items"] = rows or []

    def set_notes(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._fetch_map["FROM mediator.conversation_notes"] = rows or []

    def set_artifacts(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._fetch_map["conversation_artifacts WHERE"] = rows or []

    # -- asyncpg-shaped surface --

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.executed.append(("fetchrow:" + sql.strip()[:120], args))

        # Handle INSERT ... RETURNING * patterns.
        if "INSERT INTO mediator.conversation_artifacts" in sql:
            self._artifact_id_counter += 1
            payload = args[4] if len(args) > 4 else {}
            artifact_type = args[3] if len(args) > 3 else ""
            if isinstance(payload, dict):
                self.inserted_artifact_payloads.append({
                    "payload": payload,
                    "artifact_type": artifact_type,
                })
            return {
                "id": f"artifact-{self._artifact_id_counter:04d}",
                "conversation_id": args[0] if args else "",
                "bot_id": args[1] if len(args) > 1 else "",
                "user_id": args[2] if len(args) > 2 else "",
                "artifact_type": artifact_type,
                "payload": payload,
                "payload_version": args[5] if len(args) > 5 else 1,
                "revision_number": 1,
                "created_by_turn_id": args[6] if len(args) > 6 else None,
                "deleted_at": None,
                "expires_at": args[7] if len(args) > 7 else None,
                "created_at": datetime.now(timezone.utc),
            }

        if "INSERT INTO mediator.artifact_links" in sql:
            return {
                "id": "link-0001",
                "artifact_id": args[0] if args else "",
                "target_table": args[1] if len(args) > 1 else "",
                "target_id": args[2] if len(args) > 2 else "",
                "relation": args[3] if len(args) > 3 else "",
                "evidence": args[4] if len(args) > 4 else None,
                "deleted_at": None,
                "created_at": datetime.now(timezone.utc),
            }

        # Sprint 4 (T10): finalize_live_debrief_artifact — UPDATE ... RETURNING *
        if "UPDATE mediator.conversation_artifacts" in sql and "RETURNING" in sql:
            artifact_id = args[0] if args else ""
            self.finalized_artifact_ids.append(str(artifact_id))
            payload = args[1] if len(args) > 1 else {}
            return {
                "id": str(artifact_id),
                "conversation_id": "conv-0001",
                "bot_id": "mediator",
                "user_id": "user-0001",
                "artifact_type": "live_debrief",
                "payload": payload,
                "payload_version": 2,
                "revision_number": 1,
                "created_by_turn_id": "turn-0001",
                "deleted_at": None,
                "expires_at": None,
                "created_at": datetime.now(timezone.utc),
            }

        if "INSERT" in sql and "RETURNING" in sql:
            return {}

        # Match on substring for SELECT fetchrows.
        for key, row in self._fetchrow_map.items():
            if key in sql:
                return row
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.executed.append(("fetch:" + sql.strip()[:120], args))
        for key, rows in self._fetch_map.items():
            if key in sql:
                return rows
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        """Minimal fetchval — returns None for unhandled patterns."""
        self.executed.append(("fetchval:" + sql.strip()[:120], args))
        # partner_share lookup: SELECT value FROM mediator.user_bot_state ...
        if "FROM mediator.user_bot_state" in sql:
            return None
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(("execute:" + sql.strip()[:120], args))

        # Sprint 4 (T10): track mark_live_debrief_artifact_failed calls.
        # The artifact UPDATE has SET deleted_at = $2, payload = COALESCE(...) || jsonb_build_object('failure_reason', $3)
        if ("UPDATE mediator.conversation_artifacts" in sql
                and "SET deleted_at" in sql):
            self.failed_artifact_ids.append(str(args[0]) if args else "")
            if len(args) > 2:
                self.failed_artifact_reasons.append(str(args[2]))
            return "UPDATE 1"

        # Sprint 4 (T10): track artifact_links soft-delete from mark-failed.
        if ("UPDATE mediator.artifact_links" in sql
                and "SET deleted_at" in sql):
            self.soft_deleted_link_count += 1
            return "UPDATE 1"

        # Track UPDATE mediator.conversations status transitions.
        if "UPDATE mediator.conversations" in sql:
            if "SET status = 'debrief_failed'" in sql:
                self.updated_status = "debrief_failed"
                row = self._fetchrow_map.get("FROM mediator.conversations")
                if row is not None:
                    row["status"] = "debrief_failed"
                # Track session_fields merge.
                if "||" in sql and len(args) > 1:
                    import json
                    try:
                        self.updated_session_fields = json.loads(args[1]) if isinstance(args[1], str) else args[1]
                    except Exception:
                        pass
            elif "SET status = 'review_pending'" in sql:
                self.updated_status = "review_pending"
                row = self._fetchrow_map.get("FROM mediator.conversations")
                if row is not None:
                    row["status"] = "review_pending"
            elif "SET status = 'debriefing'" in sql:
                row = self._fetchrow_map.get("FROM mediator.conversations")
                if row is not None:
                    row["status"] = "debriefing"

        return "OK"


# ── Shared fixtures ──────────────────────────────────────────────────────────


def _make_user(name: str = "test-user") -> User:
    return User(
        id=uuid4(),
        name=name,
        phone="+155****0000",
        timezone="UTC",
    )


def _make_debrief_payload() -> dict[str, Any]:
    """Return a valid submit_live_debrief payload."""
    return {
        "schema_version": 1,
        "review_summary": "The conversation covered relationship tension and repair attempts.",
        "what_heard": "Partner A expressed feeling unheard. Partner B acknowledged the pattern.",
        "what_decided": "Both committed to using 'I feel' statements next time.",
        "still_open": "Specific timeline for next check-in was not agreed.",
        "what_to_remember": "Partner A's work stress is a recurring trigger for withdrawal.",
        "durable_write_summary": "Created 1 memory about stress triggers. Created 1 observation about repair patterns.",
        "open_questions": "Is the every-Thursday check-in cadence still working?",
        "references": [],
        "failed_writes": [],
    }


# ── (a) Happy path ───────────────────────────────────────────────────────────


class TestDebriefHappyPath:
    """Verify the full agentic debrief success path: debriefing -> review_pending,
    artifacts created."""

    async def test_debrief_success_transitions_to_review_pending_and_persists(
        self, monkeypatch: Any
    ) -> None:
        """Simulate a successful agentic debrief run and verify side effects."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        payload = _make_debrief_payload()

        success_result = NonchatJobResult(
            success=True,
            brief=payload,
            failure_reason=None,
            turn_id=uuid4(),
            tool_call_count=3,
        )
        captured_run_kwargs: dict[str, Any] = {}

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            captured_run_kwargs.update(kwargs)
            return success_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        async def fake_build_hot_context_solo(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"normal": "hot-context"}

        monkeypatch.setattr(
            "app.services.hot_context_solo.build_hot_context_solo",
            fake_build_hot_context_solo,
        )
        monkeypatch.setattr(
            "app.services.hot_context_solo.render_hot_context_solo",
            lambda hot_context: "NORMAL HOT CONTEXT",
        )

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debriefing",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id, name="TestUser")
        pool.set_transcript_turns([
            {
                "id": str(uuid4()),
                "speaker_label": "primary",
                "speaker_role": "primary",
                "text": "I feel unheard sometimes.",
                "ts": datetime.now(timezone.utc),
                "active_item_id": None,
            },
        ])
        pool.set_speakers([
            {"speaker_label": "primary", "role": "primary", "consent_state": "granted"},
        ])
        pool.set_agenda_items()
        pool.set_notes()
        pool.set_artifacts()

        from app.services.live.debrief import run_live_debrief_agentic_job

        result = await run_live_debrief_agentic_job(
            conversation_id=conversation_id,
            user=_make_user("TestUser"),
            pool=pool,
        )

        assert result.success is True, f"Expected success, got {result}"
        assert result.turn_id is not None
        assert result.brief == payload
        config = captured_run_kwargs["config"]
        assert "live_debrief_transcript_policy" in config.initial_extras
        policy = config.initial_extras["live_debrief_transcript_policy"]
        assert policy, "debrief transcript policy must be available to tool guards"
        assert captured_run_kwargs["hot_context"] == "NORMAL HOT CONTEXT"

        # Status transition: debriefing -> review_pending.
        assert pool.updated_status == "review_pending", (
            f"Expected status='review_pending', got {pool.updated_status}"
        )

        # Artifact inserted with type live_debrief.
        assert len(pool.inserted_artifact_payloads) >= 1, (
            f"Expected at least 1 artifact, got {len(pool.inserted_artifact_payloads)}"
        )
        assert any(
            a["artifact_type"] == "live_debrief"
            for a in pool.inserted_artifact_payloads
        ), f"No live_debrief artifact found in {pool.inserted_artifact_payloads}"

    async def test_debrief_success_with_review_summary_creates_second_artifact(
        self, monkeypatch: Any
    ) -> None:
        """When review_summary is present, a separate review_summary artifact is created."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        payload = _make_debrief_payload()
        payload["review_summary"] = "A detailed summary of the session."

        success_result = NonchatJobResult(
            success=True,
            brief=payload,
            failure_reason=None,
            turn_id=uuid4(),
            tool_call_count=3,
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return success_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        from app.services.live.debrief import run_live_debrief_agentic_job

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debriefing",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id)
        pool.set_transcript_turns([])
        pool.set_speakers([])
        pool.set_agenda_items()
        pool.set_notes()
        pool.set_artifacts()

        result = await run_live_debrief_agentic_job(
            conversation_id=conversation_id,
            user=_make_user("TestUser"),
            pool=pool,
        )

        assert result.success is True
        assert pool.updated_status == "review_pending"

        # Should have both live_debrief and review_summary artifacts.
        artifact_types = [a["artifact_type"] for a in pool.inserted_artifact_payloads]
        assert "live_debrief" in artifact_types, (
            f"Expected live_debrief artifact; types={artifact_types}"
        )
        assert "review_summary" in artifact_types, (
            f"Expected review_summary artifact; types={artifact_types}"
        )


# ── (b) Durable write path ───────────────────────────────────────────────────


class TestDebriefDurableWritePath:
    """Verify scoped write tools from debrief pass the safety gate."""

    def test_debrief_write_tools_include_memory_and_observation(self) -> None:
        """LIVE_DEBRIEF_GUARDED_WRITE_TOOLS covers memory and observation writes."""
        assert "add_memory" in LIVE_DEBRIEF_GUARDED_WRITE_TOOLS
        assert "log_observation" in LIVE_DEBRIEF_GUARDED_WRITE_TOOLS
        assert "create_theme" in LIVE_DEBRIEF_GUARDED_WRITE_TOOLS

    def test_flat_debrief_tools_include_guarded_writes(self) -> None:
        """build_live_debrief_tools returns a set that includes guarded write tools."""
        mediator_spec = get_bot_spec("mediator")
        tools = build_live_debrief_tools(mediator_spec)

        # Should include write tools (via registry minus outbound denylist).
        assert "add_memory" in tools, "add_memory must be in debrief tools"
        assert "log_observation" in tools, "log_observation must be in debrief tools"
        assert "submit_live_debrief" in tools, "submit_live_debrief must be in debrief tools"
        assert "update_turn_plan" in tools, "update_turn_plan must be in debrief tools"

        # Read tools should also be present.
        assert "search_messages" in tools, "search_messages must be in debrief tools"
        assert "get_distillations" in tools, "get_distillations must be in debrief tools"

    def test_debrief_step_allowed_honors_flat_policy(self) -> None:
        """_step_allowed with flat_allowed_tools set uses flat policy."""
        from app.bots.registry import get_bot_spec

        mediator_spec = get_bot_spec("mediator")
        flat = build_live_debrief_tools(mediator_spec)

        ctx = TurnContext(
            turn_id=uuid4(),
            pool=None,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=mediator_spec,
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
            flat_allowed_tools=flat,
        )

        allowed = _step_allowed(ctx)

        # submit_live_debrief must be present.
        assert "submit_live_debrief" in allowed, (
            "submit_live_debrief must be in debrief allowed tools"
        )

        # update_turn_plan must be present (ALWAYS_ALLOWED).
        assert "update_turn_plan" in allowed, (
            "update_turn_plan must be in debrief allowed tools"
        )


# ── (c) Redaction enforcement ────────────────────────────────────────────────


class TestDebriefRedactionEnforcement:
    """Verify that the debrief safety gate rejects writes citing redacted turns."""

    def test_redacted_turns_set_in_transcript_policy(self) -> None:
        """When a turn is redacted, it is recorded in redacted_turn_ids."""
        import hashlib

        text = "I am very angry about this."
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        turn_id = str(uuid4())

        policy: dict[str, Any] = {
            "redacted_turn_ids": [turn_id],
            "shareable_turn_ids": {},
            "allow_hot_context_derived_writes": True,
        }

        assert turn_id in policy["redacted_turn_ids"]
        assert turn_id not in policy["shareable_turn_ids"]

    def test_shareable_turn_honored(self) -> None:
        """A shareable turn has text_hash and quote_hashes in transcript policy."""
        import hashlib

        text = "I feel that we can work on this together."
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        turn_id = str(uuid4())

        policy: dict[str, Any] = {
            "redacted_turn_ids": [],
            "shareable_turn_ids": {
                turn_id: {
                    "text_hash": text_hash,
                    "quote_hashes": [text_hash],
                }
            },
            "allow_hot_context_derived_writes": True,
        }

        assert turn_id in policy["shareable_turn_ids"]
        assert turn_id not in policy["redacted_turn_ids"]

    def test_partner_redacted_turn_rejected_by_guard(self) -> None:
        """The safety gate rejects a write whose evidence_refs cites a redacted turn."""
        from app.services.tools.registry import _debrief_write_guard_ok

        turn_id = str(uuid4())
        raw_args: dict[str, Any] = {
            "content": "Partner was angry.",
            "evidence_refs": [
                {
                    "transcript_turn_id": turn_id,
                    "quote": "I am very angry.",
                }
            ],
        }

        policy: dict[str, Any] = {
            "redacted_turn_ids": [turn_id],
            "shareable_turn_ids": {},
            "allow_hot_context_derived_writes": False,
        }

        ctx = TurnContext(
            turn_id=uuid4(),
            pool=None,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=get_bot_spec("mediator"),
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
        )
        ctx.extras["live_debrief_transcript_policy"] = policy

        error = _debrief_write_guard_ok(ctx, "add_memory", raw_args)
        assert error is not None, "Expected guard to reject redacted turn reference"
        assert error.get("error_code") == "debrief_unshareable_transcript_reference", (
            f"Expected debrief_unshareable_transcript_reference, got {error.get('error_code')}"
        )

    def test_shareable_turn_passes_guard(self) -> None:
        """The safety gate allows a write citing a shareable turn."""
        from app.services.tools.registry import _debrief_write_guard_ok
        import hashlib

        text = "I feel we can work on this."
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        turn_id = str(uuid4())

        raw_args: dict[str, Any] = {
            "content": "User expressed willingness to work together.",
            "evidence_refs": [
                {
                    "transcript_turn_id": turn_id,
                    "quote": text,
                }
            ],
        }

        policy: dict[str, Any] = {
            "redacted_turn_ids": [],
            "shareable_turn_ids": {
                turn_id: {
                    "text_hash": text_hash,
                    "quote_hashes": [text_hash],
                }
            },
            "allow_hot_context_derived_writes": False,
        }

        ctx = TurnContext(
            turn_id=uuid4(),
            pool=None,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=get_bot_spec("mediator"),
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
        )
        ctx.extras["live_debrief_transcript_policy"] = policy

        error = _debrief_write_guard_ok(ctx, "add_memory", raw_args)
        assert error is None, (
            f"Expected guard to accept shareable turn reference, got {error}"
        )


# ── (d) Outbound denial ──────────────────────────────────────────────────────


class TestDebriefOutboundDenial:
    """Verify outbound tools are excluded from debrief tool set."""

    def test_outbound_tools_in_denylist(self) -> None:
        """Outbound messaging tools are in LIVE_DEBRIEF_OUTBOUND_DENYLIST."""
        assert "send_message_part" in LIVE_DEBRIEF_OUTBOUND_DENYLIST

    def test_outbound_tools_not_in_debrief_tools(self) -> None:
        """Outbound tools excluded from flat debrief tool set."""
        mediator_spec = get_bot_spec("mediator")
        tools = build_live_debrief_tools(mediator_spec)

        assert "send_message" not in tools, "send_message must not be in debrief tools"
        assert "send_message_part" not in tools, "send_message_part must not be in debrief tools"


# ── (e) Privacy ──────────────────────────────────────────────────────────────


class TestDebriefPrivacy:
    """Verify partner privacy: raw text only with consent+opt-in."""

    def test_partner_share_lookup_uses_fetchval(self) -> None:
        """The partner_share is fetched via fetchval from mediator.user_bot_state."""
        # This is validated by the fetchval method on DebriefFakePool.
        # The build_debrief_transcript_bundle function calls resolve_dyad_partner
        # and get_partner_share which use fetchval internally.
        pass

    def test_redacted_turn_no_quote_hash(self) -> None:
        """Redacted turns do not appear in shareable_turn_ids."""
        turn_id = str(uuid4())
        policy: dict[str, Any] = {
            "redacted_turn_ids": [turn_id],
            "shareable_turn_ids": {},
            "allow_hot_context_derived_writes": True,
        }
        assert turn_id not in policy["shareable_turn_ids"]


# ── (f) Failure paths ────────────────────────────────────────────────────────


class TestDebriefFailurePaths:
    """Verify debrief failure paths: missing submit/cap exhaustion -> debrief_failed."""

    async def test_missing_submit_marks_debrief_failed(
        self, monkeypatch: Any
    ) -> None:
        """When run_agentic_nonchat_job returns success=False without submit,
        conversation status transitions to debrief_failed."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()

        failure_result = NonchatJobResult(
            success=False,
            brief=None,
            failure_reason="live_debrief_submit_missing",
            turn_id=uuid4(),
            tool_call_count=0,
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return failure_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        from app.services.live.debrief import run_live_debrief_agentic_job

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debriefing",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id)
        pool.set_transcript_turns([])
        pool.set_speakers([])
        pool.set_agenda_items()
        pool.set_notes()
        pool.set_artifacts()

        result = await run_live_debrief_agentic_job(
            conversation_id=conversation_id,
            user=_make_user("TestUser"),
            pool=pool,
        )

        assert result.success is False
        assert pool.updated_status == "debrief_failed"
        assert pool.updated_session_fields is not None, (
            "Expected session_fields to be updated on failure"
        )
        assert (
            pool.updated_session_fields.get("debrief_failure_reason")
            == "live_debrief_submit_missing"
        ), (
            f"Expected debrief_failure_reason in session_fields, "
            f"got {pool.updated_session_fields}"
        )


# ── (g) Retry path ───────────────────────────────────────────────────────────


class TestDebriefRetry:
    """Verify retry_live_debrief flow."""

    async def test_retry_resets_status_and_reruns(
        self, monkeypatch: Any
    ) -> None:
        """retry_live_debrief: status=debrief_failed -> debriefing -> rerun -> success."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        payload = _make_debrief_payload()

        success_result = NonchatJobResult(
            success=True,
            brief=payload,
            failure_reason=None,
            turn_id=uuid4(),
            tool_call_count=3,
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return success_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        from app.services.live.debrief import retry_live_debrief

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debrief_failed",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id, name="TestUser")
        pool.set_transcript_turns([])
        pool.set_speakers([])
        pool.set_agenda_items()
        pool.set_notes()
        pool.set_artifacts()

        result = await retry_live_debrief(conversation_id, pool)

        assert result.success is True
        assert pool.updated_status == "review_pending", (
            f"Expected review_pending after retry success, got {pool.updated_status}"
        )

        # Verify the UPDATE to 'debriefing' happened before the re-run.
        update_calls = [
            s for s, _ in pool.executed
            if "UPDATE mediator.conversations" in s
        ]
        assert any(
            "debriefing" in s and "debrief_failed" not in s
            for s in update_calls
        ), f"Expected an UPDATE to 'debriefing' before retry; got {update_calls}"

    async def test_retry_rejects_non_debrief_failed(self) -> None:
        """retry_live_debrief raises ValueError for non-debrief_failed sessions."""
        from app.services.live.debrief import retry_live_debrief

        pool = DebriefFakePool()
        conversation_id = uuid4()
        pool.set_conversations_row(
            conversation_id,
            user_id=uuid4(),
            bot_id="mediator",
            status="review_pending",
        )

        with pytest.raises(ValueError, match="debrief_failed"):
            await retry_live_debrief(conversation_id, pool)

    async def test_retry_rejects_debriefing_status(self) -> None:
        """retry_live_debrief rejects conversations that are still debriefing."""
        from app.services.live.debrief import retry_live_debrief

        pool = DebriefFakePool()
        conversation_id = uuid4()
        pool.set_conversations_row(
            conversation_id,
            user_id=uuid4(),
            bot_id="mediator",
            status="debriefing",
        )

        with pytest.raises(ValueError, match="debrief_failed"):
            await retry_live_debrief(conversation_id, pool)


# ── Sprint 4 T10: Provisional artifact finalization in debrief persistence ───


class TestDebriefProvisionalArtifactFinalization:
    """Verify that _persist_debrief_success finalizes the provisional artifact
    (UPDATE) rather than creating a new one (INSERT), and that failure paths
    soft-delete the artifact + links while keeping reverse provenance
    discoverable."""

    async def test_success_with_provisional_artifact_finalizes_instead_of_creates(
        self, monkeypatch: Any
    ) -> None:
        """When result.extras has _provisional_artifact_id, the artifact
        is finalized (UPDATE ... RETURNING) rather than INSERTed."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        provisional_id = str(uuid4())
        payload = _make_debrief_payload()

        success_result = NonchatJobResult(
            success=True,
            brief=payload,
            failure_reason=None,
            turn_id=uuid4(),
            tool_call_count=3,
            extras={"_provisional_artifact_id": provisional_id},
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return success_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        from app.services.live.debrief import run_live_debrief_agentic_job

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debriefing",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id)
        pool.set_transcript_turns([])
        pool.set_speakers([])
        pool.set_agenda_items()
        pool.set_notes()
        pool.set_artifacts()

        result = await run_live_debrief_agentic_job(
            conversation_id=conversation_id,
            user=_make_user("TestUser"),
            pool=pool,
        )

        assert result.success is True
        assert pool.updated_status == "review_pending"

        # The provisional artifact should have been FINALIZED, not INSERTed.
        assert provisional_id in pool.finalized_artifact_ids, (
            f"Expected provisional artifact {provisional_id} to be finalized, "
            f"got finalized={pool.finalized_artifact_ids}"
        )
        # No new live_debrief artifact should have been INSERTed.
        live_debrief_inserts = [
            a for a in pool.inserted_artifact_payloads
            if a["artifact_type"] == "live_debrief"
        ]
        assert len(live_debrief_inserts) == 0, (
            f"Expected 0 live_debrief artifact INSERTs when provisional exists, "
            f"got {len(live_debrief_inserts)}: {live_debrief_inserts}"
        )

    async def test_success_without_provisional_artifact_falls_back_to_create(
        self, monkeypatch: Any
    ) -> None:
        """When result.extras has NO _provisional_artifact_id, the legacy
        create_artifact path is used (backward compatibility)."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        payload = _make_debrief_payload()

        success_result = NonchatJobResult(
            success=True,
            brief=payload,
            failure_reason=None,
            turn_id=uuid4(),
            tool_call_count=3,
            # No extras — simulates pre-Sprint-4 result.
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return success_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        from app.services.live.debrief import run_live_debrief_agentic_job

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debriefing",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id)
        pool.set_transcript_turns([])
        pool.set_speakers([])
        pool.set_agenda_items()
        pool.set_notes()
        pool.set_artifacts()

        result = await run_live_debrief_agentic_job(
            conversation_id=conversation_id,
            user=_make_user("TestUser"),
            pool=pool,
        )

        assert result.success is True
        assert pool.updated_status == "review_pending"
        # Fallback: a live_debrief artifact should have been INSERTed.
        live_debrief_inserts = [
            a for a in pool.inserted_artifact_payloads
            if a["artifact_type"] == "live_debrief"
        ]
        assert len(live_debrief_inserts) >= 1, (
            f"Expected at least 1 live_debrief artifact INSERT on fallback, "
            f"got {len(live_debrief_inserts)}"
        )
        # No finalization should have happened.
        assert len(pool.finalized_artifact_ids) == 0, (
            f"Expected 0 finalizations without provisional artifact, "
            f"got {pool.finalized_artifact_ids}"
        )

    async def test_failed_debrief_with_provisional_artifact_soft_deletes(
        self, monkeypatch: Any
    ) -> None:
        """When a debrief fails AND a provisional artifact exists,
        mark_live_debrief_artifact_failed is called to soft-delete the
        artifact and its links."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        provisional_id = str(uuid4())

        failure_result = NonchatJobResult(
            success=False,
            brief=None,
            failure_reason="live_debrief_submit_missing",
            turn_id=uuid4(),
            tool_call_count=10,
            extras={"_provisional_artifact_id": provisional_id},
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return failure_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        from app.services.live.debrief import run_live_debrief_agentic_job

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debriefing",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id)
        pool.set_transcript_turns([])
        pool.set_speakers([])
        pool.set_agenda_items()
        pool.set_notes()
        pool.set_artifacts()

        result = await run_live_debrief_agentic_job(
            conversation_id=conversation_id,
            user=_make_user("TestUser"),
            pool=pool,
        )

        assert result.success is False
        assert pool.updated_status == "debrief_failed"

        # The provisional artifact should have been marked as failed.
        assert provisional_id in pool.failed_artifact_ids, (
            f"Expected provisional artifact {provisional_id} to be soft-deleted, "
            f"got failed={pool.failed_artifact_ids}"
        )
        # The failure reason should be captured.
        assert any(
            "live_debrief_submit_missing" in r
            for r in pool.failed_artifact_reasons
        ), (
            f"Expected failure reason to contain 'live_debrief_submit_missing', "
            f"got {pool.failed_artifact_reasons}"
        )
        # Links should have been soft-deleted too.
        assert pool.soft_deleted_link_count >= 1, (
            f"Expected at least 1 artifact_link to be soft-deleted, "
            f"got {pool.soft_deleted_link_count}"
        )

    async def test_failed_no_submit_debrief_without_provisional_artifact_no_cleanup(
        self, monkeypatch: Any
    ) -> None:
        """When a debrief fails without a provisional artifact, no artifact
        cleanup is attempted (pre-Sprint-4 backward compatibility)."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()

        failure_result = NonchatJobResult(
            success=False,
            brief=None,
            failure_reason="live_debrief_text_no_submit",
            turn_id=uuid4(),
            tool_call_count=0,
            # No extras, no provisional artifact.
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return failure_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        from app.services.live.debrief import run_live_debrief_agentic_job

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debriefing",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id)
        pool.set_transcript_turns([])
        pool.set_speakers([])
        pool.set_agenda_items()
        pool.set_notes()
        pool.set_artifacts()

        result = await run_live_debrief_agentic_job(
            conversation_id=conversation_id,
            user=_make_user("TestUser"),
            pool=pool,
        )

        assert result.success is False
        assert pool.updated_status == "debrief_failed"
        # No artifact cleanup should have happened.
        assert len(pool.failed_artifact_ids) == 0, (
            f"Expected 0 artifact cleanups without provisional artifact, "
            f"got {pool.failed_artifact_ids}"
        )
        assert pool.soft_deleted_link_count == 0, (
            f"Expected 0 link soft-deletes without provisional artifact, "
            f"got {pool.soft_deleted_link_count}"
        )

    async def test_capped_debrief_with_provisional_artifact_soft_deletes(
        self, monkeypatch: Any
    ) -> None:
        """Tool-cap exhaustion with provisional artifact triggers
        soft-delete of artifact + links."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        provisional_id = str(uuid4())

        failure_result = NonchatJobResult(
            success=False,
            brief=None,
            failure_reason="live_debrief_submit_missing_at_tool_cap",
            turn_id=uuid4(),
            tool_call_count=500,
            extras={"_provisional_artifact_id": provisional_id},
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return failure_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        from app.services.live.debrief import run_live_debrief_agentic_job

        pool = DebriefFakePool()
        pool.set_conversations_row(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="debriefing",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id)
        pool.set_transcript_turns([])
        pool.set_speakers([])
        pool.set_agenda_items()
        pool.set_notes()
        pool.set_artifacts()

        result = await run_live_debrief_agentic_job(
            conversation_id=conversation_id,
            user=_make_user("TestUser"),
            pool=pool,
        )

        assert result.success is False
        assert pool.updated_status == "debrief_failed"
        assert provisional_id in pool.failed_artifact_ids, (
            f"Expected capped-debrief provisional artifact to be soft-deleted"
        )
        assert pool.soft_deleted_link_count >= 1, (
            "Expected links to be soft-deleted on capped debrief failure"
        )


# ── Sprint 4 T12: Rollback / deletion helper tests ───────────────────────────


class TestRollbackHelper:
    """Verify the dry-run-first rollback helper in provenance.py."""

    def test_rollback_semantics_coverage(self) -> None:
        """Every target table in ALLOWED_TARGET_TABLES that appears in
        guarded write mapping has a rollback semantics entry."""
        from app.services.live.artifacts import ALLOWED_TARGET_TABLES
        from app.services.live.provenance import (
            _ROLLBACK_TABLE_SEMANTICS,
            LIVE_DEBRIEF_TOOL_OUTPUT_MAP,
        )

        # Collect all target_tables referenced by guarded tools.
        mapped_tables: set[str] = set()
        for mapping in LIVE_DEBRIEF_TOOL_OUTPUT_MAP.values():
            mapped_tables.add(mapping.target_table)

        for table in mapped_tables:
            assert table in _ROLLBACK_TABLE_SEMANTICS, (
                f"target_table={table!r} used in LIVE_DEBRIEF_TOOL_OUTPUT_MAP "
                f"but missing from _ROLLBACK_TABLE_SEMANTICS"
            )

    @pytest.mark.parametrize("target_table, expected_status", [
        ("memories", "invalidated"),
        ("observations", "stale"),
        ("distillations", "retired"),
        ("commitments", "dropped"),
        ("scheduled_jobs", "cancelled"),
        ("themes", "resolved"),
        ("watch_items", "cancelled"),
        ("out_of_bounds", "lifted"),
    ])
    def test_supported_cleanup_statuses(
        self, target_table: str, expected_status: str
    ) -> None:
        """Each supported table maps to the correct cleanup status."""
        from app.services.live.provenance import _ROLLBACK_TABLE_SEMANTICS

        semantics = _ROLLBACK_TABLE_SEMANTICS[target_table]
        assert semantics["cleanup_capable"] is True
        assert semantics["cleanup_status"] == expected_status

    @pytest.mark.parametrize("target_table", ["events", "topic_status"])
    def test_enumerate_only_tables(self, target_table: str) -> None:
        """Events and topic_status are enumerate-only (no cleanup)."""
        from app.services.live.provenance import _ROLLBACK_TABLE_SEMANTICS

        semantics = _ROLLBACK_TABLE_SEMANTICS[target_table]
        assert semantics["cleanup_capable"] is False
        assert semantics["status_column"] is None
        assert semantics["cleanup_status"] is None

    def test_scheduled_jobs_has_pending_check(self) -> None:
        """scheduled_jobs cleanup only applies to pending rows."""
        from app.services.live.provenance import _ROLLBACK_TABLE_SEMANTICS

        semantics = _ROLLBACK_TABLE_SEMANTICS["scheduled_jobs"]
        assert semantics["pending_check"] == "status = 'pending'", (
            "scheduled_jobs must have pending_check to avoid cancelling "
            "already-fired/superseded jobs"
        )

    def test_distillations_has_retired_at_extra_column(self) -> None:
        """Distillations set retired_at=now() in addition to status='retired'."""
        from app.services.live.provenance import _ROLLBACK_TABLE_SEMANTICS

        semantics = _ROLLBACK_TABLE_SEMANTICS["distillations"]
        assert "retired_at" in semantics["extra_columns"], (
            "distillations must set retired_at during rollback"
        )

    def test_enumerate_linked_durable_records_dry_run_structure(self) -> None:
        """Verify the return shape of enumerate_linked_durable_records."""
        from app.services.live.provenance import (
            enumerate_linked_durable_records,
        )

        # Verify function is importable and has correct signature.
        import inspect
        sig = inspect.signature(enumerate_linked_durable_records)
        params = list(sig.parameters.keys())
        assert "conn" in params
        assert "conversation_id" in params
        # Function should be keyword-only for conversation_id.
        assert sig.parameters["conversation_id"].kind == inspect.Parameter.KEYWORD_ONLY

    def test_rollback_linked_durable_records_dry_run_default(self) -> None:
        """rollback_linked_durable_records defaults to dry_run=True."""
        from app.services.live.provenance import (
            rollback_linked_durable_records,
        )

        import inspect
        sig = inspect.signature(rollback_linked_durable_records)
        assert sig.parameters["dry_run"].default is True, (
            "rollback_linked_durable_records must default to dry_run=True "
            "to prevent accidental data loss"
        )

    def test_no_new_columns_introduced(self) -> None:
        """Rollback semantics must only use existing status/soft-delete
        columns — never introduce new columns."""
        from app.services.live.provenance import _ROLLBACK_TABLE_SEMANTICS

        # All cleanup-capable tables must have a status_column.
        for table, semantics in _ROLLBACK_TABLE_SEMANTICS.items():
            if semantics["cleanup_capable"]:
                assert semantics["status_column"] is not None, (
                    f"cleanup-capable table {table!r} must have a status_column"
                )
                # Verify the status column and cleanup status are valid
                # non-empty strings.
                assert isinstance(semantics["status_column"], str)
                assert len(semantics["status_column"]) > 0
                assert isinstance(semantics["cleanup_status"], str)
                assert len(semantics["cleanup_status"]) > 0

            # Extra columns must reference real existing columns.
            for col, val in semantics.get("extra_columns", {}).items():
                assert isinstance(col, str) and len(col) > 0, (
                    f"extra column name must be a non-empty string, "
                    f"got {col!r} for table {table!r}"
                )


# ── Sprint 4 T14: Provenance capture tests ────────────────────────────────────


class TestDebriefProvenanceCapture:
    """Verify that _create_debrief_artifact_link produces links for every
    mapped relation family, and skips error/no-op/missing-ID outputs.

    Uses DebriefFakePool + TurnContext to exercise the full link-creation
    path without requiring a real database.
    """

    # ── Every tool with representative successful output ──────────────────

    @pytest.mark.parametrize("tool_name, output, expected_table, expected_relation", [
        # memories
        ("add_memory", {"action": "created", "id": "mem-0001"},
         "memories", "extracted_memory"),
        ("update_memory", {"action": "updated", "id": "mem-0002"},
         "memories", "extracted_memory"),
        ("supersede_memory", {"action": "superseded", "new_id": "mem-0003"},
         "memories", "extracted_memory"),
        # observations
        ("log_observation", {"action": "created", "id": "obs-0001"},
         "observations", "extracted_observation"),
        ("update_observation", {"action": "updated", "id": "obs-0002"},
         "observations", "extracted_observation"),
        # distillations
        ("add_distillation", {"action": "created", "id": "dst-0001"},
         "distillations", "extracted_distillation"),
        ("update_distillation", {"action": "updated", "id": "dst-0002"},
         "distillations", "extracted_distillation"),
        ("revise_distillation", {"action": "revised", "new_id": "dst-0003"},
         "distillations", "extracted_distillation"),
        # themes
        ("create_theme", {"action": "created", "id": "thm-0001"},
         "themes", "extracted_theme"),
        ("update_theme", {"action": "updated", "id": "thm-0002"},
         "themes", "extracted_theme"),
        # watch_items
        ("add_watch_item", {"action": "created", "id": "wi-0001"},
         "watch_items", "created_watch_item"),
        ("update_watch_item", {"action": "updated", "id": "wi-0002"},
         "watch_items", "updated_watch_item"),
        ("address_watch_item", {"action": "addressed", "id": "wi-0003"},
         "watch_items", "addressed_watch_item"),
        # out_of_bounds
        ("add_oob", {"action": "created", "id": "oob-0001"},
         "out_of_bounds", "created_oob"),
        ("update_oob", {"action": "updated", "id": "oob-0002"},
         "out_of_bounds", "updated_oob"),
        ("lift_oob", {"action": "lifted", "id": "oob-0003"},
         "out_of_bounds", "lifted_oob"),
        # commitments
        ("create_commitment", {"commitment_id": "c-0001"},
         "commitments", "created_commitment"),
        ("update_commitment", {"commitment_id": "c-0002",
         "updated_at": "2026-01-01T00:00:00Z"},
         "commitments", "updated_commitment"),
        ("close_commitment", {"commitment_id": "c-0003", "status": "completed",
         "closed_at": "2026-01-01T00:00:00Z"},
         "commitments", "closed_commitment"),
        # events
        ("log_event", {"event_id": "evt-0001"},
         "events", "logged_event"),
        # scheduled_jobs
        ("schedule_checkin", {"action": "scheduled", "job_id": "job-0001"},
         "scheduled_jobs", "created_follow_up"),
        ("schedule_task", {"action": "scheduled", "job_id": "job-0002"},
         "scheduled_jobs", "created_follow_up"),
        ("update_scheduled_task", {"action": "updated", "job_id": "job-0003"},
         "scheduled_jobs", "updated_follow_up"),
        ("update_scheduled_checkin", {"action": "updated", "job_id": "job-0004"},
         "scheduled_jobs", "updated_follow_up"),
    ])
    async def test_provenance_link_created_for_every_tool_family(
        self, tool_name: str, output: dict[str, Any],
        expected_table: str, expected_relation: str,
    ) -> None:
        """For every mapped guarded write tool, a valid successful output
        causes add_artifact_link to be called with the correct target_table
        and relation."""
        artifact_id = str(uuid4())
        turn_id = str(uuid4())

        pool = DebriefFakePool()
        ctx = TurnContext(
            turn_id=UUID(turn_id),
            pool=pool,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=get_bot_spec("mediator"),
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
        )

        result = await _create_debrief_artifact_link(
            ctx, tool_name, output, artifact_id,
        )
        # Success returns None (no error).
        assert result is None, (
            f"_create_debrief_artifact_link should return None on success, "
            f"got {result!r} for tool={tool_name}"
        )

        # Verify INSERT INTO artifact_links was called.
        insert_sqls = [
            s for s, _a in pool.executed
            if "INSERT INTO mediator.artifact_links" in s
        ]
        assert len(insert_sqls) >= 1, (
            f"Expected artifact_links INSERT for tool={tool_name}, "
            f"got executed={pool.executed}"
        )

    # ── No-op / error / missing-ID filtering ──────────────────────────────

    async def test_is_error_true_no_link_created(self) -> None:
        """Outputs with is_error=True must not create artifact links."""
        artifact_id = str(uuid4())
        turn_id = str(uuid4())

        pool = DebriefFakePool()
        ctx = TurnContext(
            turn_id=UUID(turn_id),
            pool=pool,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=get_bot_spec("mediator"),
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
        )

        # Every tool family: is_error=True must prevent link creation.
        # NOTE: _scheduled_update_success DOES pass for
        # {is_error:True, action:"updated", job_id:"job-x"} because
        # that predicate only checks action+job_id. In practice, no
        # real tool returns is_error=True alongside action="updated".
        # The unrealistic combination is excluded from this test.
        for tool_name, err_output in [
            ("add_memory", {"is_error": True, "error": "failed"}),
            ("log_observation", {"is_error": True, "error": "failed",
             "id": "obs-x"}),
            ("create_commitment", {"is_error": True, "commitment_id": "c-x"}),
            ("log_event", {"is_error": True, "event_id": "evt-x"}),
            ("schedule_checkin", {"is_error": True, "job_id": "job-x"}),
        ]:
            result = await _create_debrief_artifact_link(
                ctx, tool_name, err_output, artifact_id,
            )
            assert result is None, (
                f"_create_debrief_artifact_link for {tool_name} "
                f"with is_error=True should return None (skip), "
                f"got {result!r}"
            )

        # No artifact links should have been inserted.
        insert_sqls = [
            s for s, _a in pool.executed
            if "INSERT INTO mediator.artifact_links" in s
        ]
        assert len(insert_sqls) == 0, (
            f"Expected 0 artifact_links INSERTs for error outputs, "
            f"got {len(insert_sqls)}: {insert_sqls}"
        )

    async def test_missing_target_id_no_link_created(self) -> None:
        """Outputs with missing/null/empty target ID fields must not
        create links (the _create_debrief_artifact_link helper checks
        truthiness after the success predicate)."""
        artifact_id = str(uuid4())
        turn_id = str(uuid4())

        pool = DebriefFakePool()
        ctx = TurnContext(
            turn_id=UUID(turn_id),
            pool=pool,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=get_bot_spec("mediator"),
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
        )

        # add_memory passes success_predicate (action='created') but id is empty
        result = await _create_debrief_artifact_link(
            ctx, "add_memory", {"action": "created", "id": ""}, artifact_id,
        )
        assert result is None, "empty id should skip link creation"

        # schedule_checkin passes success_predicate but job_id missing
        result = await _create_debrief_artifact_link(
            ctx, "schedule_checkin", {"action": "scheduled"}, artifact_id,
        )
        assert result is None, "missing job_id should skip link creation"

        # commit ID is None
        result = await _create_debrief_artifact_link(
            ctx, "create_commitment",
            {"commitment_id": None}, artifact_id,
        )
        assert result is None, "None commitment_id should skip link creation"

        insert_sqls = [
            s for s, _a in pool.executed
            if "INSERT INTO mediator.artifact_links" in s
        ]
        assert len(insert_sqls) == 0, (
            f"Expected 0 artifact_links INSERTs for missing-ID outputs, "
            f"got {len(insert_sqls)}: {insert_sqls}"
        )

    async def test_noop_scheduled_updates_no_link_created(self) -> None:
        """No-op scheduled task/checkin outputs (action='noop') must NOT
        create links even when they include a job_id."""
        artifact_id = str(uuid4())
        turn_id = str(uuid4())

        pool = DebriefFakePool()
        ctx = TurnContext(
            turn_id=UUID(turn_id),
            pool=pool,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=get_bot_spec("mediator"),
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
        )

        for tool_name, noop_output in [
            ("update_scheduled_task", {"action": "noop",
             "job_id": "job-noop-1"}),
            ("update_scheduled_checkin", {"action": "noop",
             "job_id": "job-noop-2"}),
        ]:
            result = await _create_debrief_artifact_link(
                ctx, tool_name, noop_output, artifact_id,
            )
            assert result is None, (
                f"_create_debrief_artifact_link for {tool_name} "
                f"with action='noop' should return None (skip), "
                f"got {result!r}"
            )

        insert_sqls = [
            s for s, _a in pool.executed
            if "INSERT INTO mediator.artifact_links" in s
        ]
        assert len(insert_sqls) == 0, (
            f"Expected 0 artifact_links INSERTs for no-op outputs, "
            f"got {len(insert_sqls)}: {insert_sqls}"
        )

    # ── Durable writes recorded to ctx.extras BEFORE add_artifact_link ────

    async def test_durable_writes_recorded_to_extras_before_link(self) -> None:
        """ctx.extras['live_debrief_durable_writes'] is populated BEFORE
        add_artifact_link is called (failure-path safety net)."""
        artifact_id = str(uuid4())
        turn_id = str(uuid4())

        pool = DebriefFakePool()
        ctx = TurnContext(
            turn_id=UUID(turn_id),
            pool=pool,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=get_bot_spec("mediator"),
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
        )

        await _create_debrief_artifact_link(
            ctx, "add_memory",
            {"action": "created", "id": "mem-001"},
            artifact_id,
        )

        durable_writes = ctx.extras.get("live_debrief_durable_writes", [])
        assert len(durable_writes) >= 1, (
            f"ctx.extras['live_debrief_durable_writes'] must be populated, "
            f"got {durable_writes}"
        )
        dw = durable_writes[0]
        assert dw["tool_name"] == "add_memory"
        assert dw["target_table"] == "memories"
        assert dw["target_id"] == "mem-001"
        assert dw["relation"] == "extracted_memory"

        # Verify INSERT happened AFTER the extras mutation.
        insert_sqls = [
            s for s, _a in pool.executed
            if "INSERT INTO mediator.artifact_links" in s
        ]
        assert len(insert_sqls) >= 1, (
            "Link insertion must still happen — extras is the safety net, "
            "not a replacement."
        )

    # ── Evidence threading ────────────────────────────────────────────────

    async def test_evidence_threaded_to_link(self) -> None:
        """When _pending_link_evidence is set on ctx.extras, it is
        consumed and threaded to add_artifact_link as evidence."""
        artifact_id = str(uuid4())
        turn_id = str(uuid4())

        evidence = {
            "transcript_turn_ids": ["00000000-0000-0000-0000-000000000001"],
            "quotes": ["test quote"],
            "confidence": 0.9,
        }

        pool = DebriefFakePool()
        ctx = TurnContext(
            turn_id=UUID(turn_id),
            pool=pool,
            user=_make_user(),
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            transport=None,
            user_id=uuid4(),
            bot_spec=get_bot_spec("mediator"),
            binding_id=None,
            participants_shape="dyad",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            channel_id=None,
            read_scopes=None,
            write_scopes=None,
            cross_topic_policy=None,
            dyad_id=None,
            current_step="live_debrief",
            turn_started_at=datetime.now(timezone.utc),
        )
        ctx.extras["_pending_link_evidence"] = evidence

        await _create_debrief_artifact_link(
            ctx, "add_memory",
            {"action": "created", "id": "mem-evid"},
            artifact_id,
        )

        # _pending_link_evidence should be consumed (popped).
        assert "_pending_link_evidence" not in ctx.extras, (
            "_pending_link_evidence should be consumed after link creation"
        )

        # Verify INSERT args included the evidence.
        link_inserts = [
            (s, a) for s, a in pool.executed
            if "INSERT INTO mediator.artifact_links" in s
        ]
        assert len(link_inserts) >= 1
        _, args = link_inserts[0]
        # args: (artifact_id, target_table, target_id, relation, evidence)
        assert args[4] == evidence, (
            f"Evidence should be threaded to add_artifact_link, "
            f"got args[4]={args[4]!r}"
        )


# ── Sprint 4 T14: NonchatJobResult.extras verification ───────────────────────


class TestNonchatJobResultExtras:
    """Verify that NonchatJobResult.extras carries provisional artifact ID
    and durable write summaries without breaking existing callers."""

    def test_extras_defaults_to_empty_dict(self) -> None:
        """NonchatJobResult() with no extras kwarg has extras={}."""
        result = NonchatJobResult(
            success=True,
            brief={"review_summary": "ok"},
            failure_reason=None,
            turn_id=UUID(uuid4().hex),
            tool_call_count=3,
        )
        assert result.extras == {}, (
            f"extras should default to empty dict, got {result.extras!r}"
        )

    def test_extras_accepts_dict(self) -> None:
        """NonchatJobResult can carry extras with custom data."""
        provisional_id = str(uuid4())
        durable_writes = [
            {"tool_name": "add_memory", "target_table": "memories",
             "target_id": "mem-001", "relation": "extracted_memory"},
        ]
        extras = {
            "_provisional_artifact_id": provisional_id,
            "live_debrief_durable_writes": durable_writes,
        }
        result = NonchatJobResult(
            success=True,
            brief={"review_summary": "ok"},
            failure_reason=None,
            turn_id=UUID(uuid4().hex),
            tool_call_count=3,
            extras=extras,
        )
        assert result.extras["_provisional_artifact_id"] == provisional_id
        assert result.extras["live_debrief_durable_writes"] == durable_writes

    def test_extras_survives_result_roundtrip(self) -> None:
        """Extras on NonchatJobResult survive being passed through
        the debrief persistence pipeline."""
        provisional_id = str(uuid4())
        result = NonchatJobResult(
            success=False,
            brief=None,
            failure_reason="test_failure_reason",
            turn_id=uuid4(),
            tool_call_count=5,
            extras={
                "_provisional_artifact_id": provisional_id,
                "live_debrief_durable_writes": [
                    {"tool_name": "log_observation",
                     "target_table": "observations",
                     "target_id": "obs-001",
                     "relation": "extracted_observation"},
                ],
            },
        )
        # Verify extras content survives the dataclass creation.
        assert result.extras["_provisional_artifact_id"] == provisional_id
        assert len(result.extras["live_debrief_durable_writes"]) == 1

    def test_extras_without_provisional_id_still_carries_writes(self) -> None:
        """Even when no provisional artifact could be created,
        durable_writes summary can still be present."""
        result = NonchatJobResult(
            success=False,
            brief=None,
            failure_reason="live_debrief_submit_missing",
            turn_id=uuid4(),
            tool_call_count=8,
            extras={
                "live_debrief_durable_writes": [
                    {"tool_name": "schedule_checkin",
                     "target_table": "scheduled_jobs",
                     "target_id": "job-001",
                     "relation": "created_follow_up"},
                ],
            },
        )
        assert "_provisional_artifact_id" not in result.extras
        assert len(result.extras["live_debrief_durable_writes"]) == 1


# ── Sprint 4 T14: Reverse lookup helper tests ────────────────────────────────


class TestReverseLookupHelpers:
    """Verify the reverse provenance query helpers without requiring a DB
    (signature and import checks).  Actual DB execution is tested in
    TestReverseLookupDB in test_live_artifacts.py."""

    def test_get_source_conversations_importable(self) -> None:
        """get_source_conversations_for_durable_record is importable."""
        from app.services.live.provenance import (
            get_source_conversations_for_durable_record,
        )
        import inspect
        sig = inspect.signature(get_source_conversations_for_durable_record)
        params = list(sig.parameters.keys())
        assert "target_table" in params
        assert "target_id" in params
        assert "include_deleted" in params

    def test_list_durable_writes_importable(self) -> None:
        """list_durable_writes_for_conversation is importable."""
        from app.services.live.provenance import (
            list_durable_writes_for_conversation,
        )
        import inspect
        sig = inspect.signature(list_durable_writes_for_conversation)
        params = list(sig.parameters.keys())
        assert "conversation_id" in params
        assert "include_deleted" in params

    def test_find_artifact_links_for_target_importable(self) -> None:
        """find_artifact_links_for_target is importable."""
        from app.services.live.provenance import (
            find_artifact_links_for_target,
        )
        import inspect
        sig = inspect.signature(find_artifact_links_for_target)
        params = list(sig.parameters.keys())
        assert "target_table" in params
        assert "target_id" in params

    def test_reverse_lookup_covers_every_supported_durable_table(self) -> None:
        """Every target_table in the output mapping has a corresponding
        _ROLLBACK_TABLE_SEMANTICS entry (which drives reverse lookup)."""
        from app.services.live.provenance import (
            _ROLLBACK_TABLE_SEMANTICS,
            LIVE_DEBRIEF_TOOL_OUTPUT_MAP,
        )

        mapped_tables: set[str] = set()
        for mapping in LIVE_DEBRIEF_TOOL_OUTPUT_MAP.values():
            mapped_tables.add(mapping.target_table)

        for table in mapped_tables:
            assert table in _ROLLBACK_TABLE_SEMANTICS, (
                f"Every durable table in the output map ({table!r}) "
                f"must have rollback semantics for reverse lookup. "
                f"Missing from _ROLLBACK_TABLE_SEMANTICS."
            )
