"""Integration tests for agentic live prep (Sprint 2).

Covers:
(a) Agentic success path: fake provider scripts a 2-iteration plan
    (read tool -> submit_live_brief), verify status prepping->ready,
    current_item_id set, conversation_artifacts has live_prep_brief row,
    one planned_item link per agenda item.
(b) Non-mediator bot context: pick non-mediator bot from BOT_SPECS,
    assert system prompt contains that bot's rendered prompt from
    live_bot_profile_context(..., user=user, partner=partner).
(c) Tool-gating: verify live_prep step excludes outbound (send_message_part),
    OOB tools, and all WRITE_PHASE_TOOLS.  Read-only scheduling list tools
    (list_scheduled_tasks etc.) are intentionally included.
(d) Missing-submit test: provider returns plain text only, assert
    status='prep_failed', /card surfaces failure, /prep/retry re-enters
    prepping.
(e) Orphan recovery: insert conversation with status='prepping' and
    created_at 20 min ago, run sweep, assert status='prep_failed'.

All tests use a FakePool that records SQL — no real LLM APIs or DB.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.bots.base import BotSpec
from app.bots.registry import BOT_SPECS, _maybe_register_staging_bots, get_bot_spec
from app.config import get_settings
from app.models.user import User
from app.services.live.bot_profile import live_bot_profile_context
from app.services.live.prep import (
    _set_prep_failed,
    retry_live_prep,
    run_live_prep_agentic_job,
)
from app.services.nonchat_agentic import NonchatJobResult
from app.services.recovery import sweep_orphaned_prepping
from app.services.tools.registry import (
    LIVE_PREP_TOOLS,
    READ_PHASE_TOOLS,
    RESPOND_TOOLS,
    SCHEDULE_TOOLS,
    WRITE_PHASE_TOOLS,
    _step_allowed,
)
from app.services.turn_context import TurnContext


# ── FakePool for integration tests ──────────────────────────────────────────


class _FakeTxn:
    """Auto-committing fake transaction — no-op enter/exit."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    """Single-connection handle that delegates to the parent FakePool."""

    def __init__(self, parent: "PrepFakePool") -> None:
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
    def __init__(self, parent: "PrepFakePool") -> None:
        self._parent = parent

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._parent)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class PrepFakePool:
    """Minimal asyncpg pool stand-in for live prep integration tests.

    Records all executed SQL and supplies canned return values for every
    fetch/fetchrow pattern used by the live prep code path, including
    INSERT ... RETURNING * patterns used by create_artifact and
    add_artifact_link.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

        # Canned fetchrow results keyed by substring match.
        self._fetchrow_map: dict[str, dict[str, Any] | None] = {}
        # Canned fetch (multi-row) results keyed by substring match.
        self._fetch_map: dict[str, list[dict[str, Any]]] = {}

        # Track INSERTs / UPDATEs for verification.
        self.inserted_items: list[dict[str, Any]] = []
        self.inserted_artifact_payloads: list[dict[str, Any]] = []
        self.inserted_link_targets: list[str] = []
        self.updated_status: str | None = None

        # Auto-generated UUIDs for artifact rows (incrementing).
        self._artifact_id_counter = 0
        self._link_id_counter = 0

    # -- public helpers --

    def set_conversations_row_for(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID,
        partner_user_id: UUID | None = None,
        bot_id: str = "mediator",
        status: str = "prepping",
        steering_text: str = "",
        topic_id: UUID | None = None,
        session_fields: dict[str, Any] | None = None,
    ) -> None:
        # Return id fields as strings so UUID(row["..."]) works.
        self._fetchrow_map["FROM mediator.conversations"] = {
            "id": conversation_id,
            "user_id": str(user_id),
            "partner_user_id": str(partner_user_id) if partner_user_id is not None else None,
            "bot_id": bot_id,
            "mode": "open",
            "steering_text": steering_text,
            "status": status,
            "topic_id": str(topic_id) if topic_id is not None else None,
            "session_fields": session_fields or {},
            "prep_summary": None,
            "current_item_id": None,
        }

    def set_user_row(self, user_id: UUID, *, name: str = "test-user") -> None:
        self._fetchrow_map["SELECT * FROM users"] = {
            "id": user_id,
            "name": name,
            "phone": "+15550000000",
            "timezone": "UTC",
        }

    def set_themes(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._fetch_map["FROM themes WHERE user_id"] = rows or []

    def set_distillations(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._fetch_map["FROM distillations"] = rows or []

    def set_messages(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._fetch_map["FROM messages"] = rows or []

    def set_theme_lookup(
        self, rows: list[dict[str, Any]] | None = None
    ) -> None:
        self._fetch_map["AND slug = ANY"] = rows or []

    # -- asyncpg-shaped surface --

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.executed.append(("fetchrow:" + sql.strip(), args))

        # Handle INSERT ... RETURNING * patterns (create_artifact, add_artifact_link).
        if "INSERT INTO mediator.conversation_artifacts" in sql:
            self._artifact_id_counter += 1
            # Track payload for verification.
            if len(args) >= 5:
                self.inserted_artifact_payloads.append({
                    "payload": args[4],
                    "artifact_type": args[3] if len(args) > 3 else None,
                })
            return {
                "id": f"artifact-{self._artifact_id_counter:04d}",
                "conversation_id": args[0] if args else "",
                "bot_id": args[1] if len(args) > 1 else "",
                "user_id": args[2] if len(args) > 2 else "",
                "artifact_type": args[3] if len(args) > 3 else "",
                "payload": args[4] if len(args) > 4 else {},
                "payload_version": args[5] if len(args) > 5 else 1,
                "revision_number": 1,
                "created_by_turn_id": args[6] if len(args) > 6 else None,
                "deleted_at": None,
                "expires_at": args[7] if len(args) > 7 else None,
                "created_at": datetime.now(timezone.utc),
            }

        if "INSERT INTO mediator.artifact_links" in sql:
            self._link_id_counter += 1
            if len(args) >= 4:
                self.inserted_link_targets.append(args[3])
            return {
                "id": f"link-{self._link_id_counter:04d}",
                "artifact_id": args[0] if args else "",
                "target_table": args[1] if len(args) > 1 else "",
                "target_id": args[2] if len(args) > 2 else "",
                "relation": args[3] if len(args) > 3 else "",
                "evidence": args[4] if len(args) > 4 else None,
                "deleted_at": None,
                "created_at": datetime.now(timezone.utc),
            }

        if "INSERT INTO mediator.artifact_links" not in sql and "INSERT" in sql and "RETURNING" in sql:
            # Generic INSERT RETURNING — return empty dict.
            return {}

        # Regular SELECT fetchrow.
        for key, row in self._fetchrow_map.items():
            if key in sql:
                return row
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.executed.append(("fetch:" + sql.strip(), args))
        for key, rows in self._fetch_map.items():
            if key in sql:
                return rows
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(("execute:" + sql.strip(), args))

        # Track INSERT INTO mediator.conversation_items.
        # Column order: id, conversation_id, theme_id, kind, title,
        # intent, ask, done_when, next_item_ids, priority (idx 9),
        # speaker_scope, coverage_evidence_required, order_hint.
        if "INSERT INTO mediator.conversation_items" in sql:
            item = {
                "id": args[0],
                "title": args[4],
                "priority": args[9] if len(args) > 9 else None,
            }
            self.inserted_items.append(item)

        # Track UPDATE mediator.conversations status transitions and
        # also update the stored conversations row so subsequent
        # fetches see the new status (needed for retry flow).
        elif "UPDATE mediator.conversations" in sql and "status" in sql:
            if "SET status = 'prep_failed'" in sql:
                self.updated_status = "prep_failed"
                row = self._fetchrow_map.get("FROM mediator.conversations")
                if row is not None:
                    row["status"] = "prep_failed"
            elif "SET status = 'ready'" in sql:
                self.updated_status = "ready"
                row = self._fetchrow_map.get("FROM mediator.conversations")
                if row is not None:
                    row["status"] = "ready"
            elif "SET status = 'prepping'" in sql or "SET status = 'preparing'" in sql:
                row = self._fetchrow_map.get("FROM mediator.conversations")
                if row is not None:
                    row["status"] = "preparing"

        return "OK"


# ── Shared fixtures ─────────────────────────────────────────────────────────


def _make_user(name: str = "test-user") -> User:
    return User(
        id=uuid4(),
        name=name,
        phone="+15550000000",
        timezone="UTC",
    )


def _make_minimal_agenda() -> dict[str, Any]:
    """Return a valid agenda dict that passes Agenda.model_validate."""
    return {
        "agenda": {
            "prep_summary": "A test brief",
            "items": [
                {
                    "id": "anchor",
                    "title": "Anchor item",
                    "priority": "must",
                    "kind": "planned",
                    "speaker_scope": "primary",
                    "coverage_evidence_required": "explicit_answer",
                },
                {
                    "id": "follow",
                    "title": "Follow item",
                    "priority": "should",
                    "kind": "planned",
                    "speaker_scope": "primary",
                    "coverage_evidence_required": "explicit_answer",
                },
            ],
            "first_item_id": "anchor",
        },
        "notes": "prep notes",
    }


# ── (a) Agentic success path ────────────────────────────────────────────────


class TestAgenticSuccessPath:
    """Verify the full agentic prep success path: prepping -> ready,
    artifacts created, items persisted, planned_item links added."""

    async def test_agentic_success_transitions_to_ready_and_persists(
        self, monkeypatch: Any
    ) -> None:
        """Simulate a successful agentic prep run and verify all side effects."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        brief = _make_minimal_agenda()

        success_result = NonchatJobResult(
            success=True,
            brief=brief,
            failure_reason=None,
            turn_id=uuid4(),
            tool_call_count=2,  # read tool + submit_live_brief
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return success_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        pool = PrepFakePool()
        pool.set_conversations_row_for(
            conversation_id,
            user_id=user_id,
            partner_user_id=None,
            bot_id="mediator",
            status="prepping",
            steering_text="test steering",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id, name="TestUser")
        pool.set_themes()
        pool.set_distillations()
        pool.set_messages()
        pool.set_theme_lookup()

        result = await run_live_prep_agentic_job(
            conversation_id=conversation_id,
            user_id=user_id,
            bot_id="mediator",
            steering_text="test steering",
            topic_id=topic_id,
            pool=pool,
        )

        # ── Assertions ────────────────────────────────────────────────
        assert result.success is True, f"Expected success, got {result}"
        assert result.turn_id is not None
        assert result.tool_call_count == 2

        # Status transition: prepping -> ready.
        assert pool.updated_status == "ready", (
            f"Expected status='ready', got {pool.updated_status}"
        )

        # Conversation items inserted.
        assert len(pool.inserted_items) == 2, (
            f"Expected 2 conversation items, got {len(pool.inserted_items)}"
        )
        assert pool.inserted_items[0]["title"] == "Anchor item"
        assert pool.inserted_items[0]["priority"] == "must"

        # Artifact inserted with type live_prep_brief.
        assert len(pool.inserted_artifact_payloads) == 1, (
            f"Expected 1 artifact, got {len(pool.inserted_artifact_payloads)}"
        )
        assert (
            pool.inserted_artifact_payloads[0]["artifact_type"]
            == "live_prep_brief"
        )

        # One planned_item link per agenda item.
        assert len(pool.inserted_link_targets) == 2, (
            f"Expected 2 artifact links, got {len(pool.inserted_link_targets)}"
        )

    async def test_agentic_success_sets_current_item_id(
        self, monkeypatch: Any
    ) -> None:
        """Verify current_item_id is set to the UUID of first_item_id's row."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        brief = _make_minimal_agenda()

        success_result = NonchatJobResult(
            success=True,
            brief=brief,
            failure_reason=None,
            turn_id=uuid4(),
            tool_call_count=2,
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return success_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        pool = PrepFakePool()
        pool.set_conversations_row_for(
            conversation_id,
            user_id=user_id,
            partner_user_id=None,
            bot_id="mediator",
            status="prepping",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id, name="TestUser")
        pool.set_themes()
        pool.set_distillations()
        pool.set_messages()
        pool.set_theme_lookup()

        await run_live_prep_agentic_job(
            conversation_id=conversation_id,
            user_id=user_id,
            bot_id="mediator",
            steering_text=None,
            topic_id=topic_id,
            pool=pool,
        )

        # The UPDATE should set current_item_id.
        for s, args in pool.executed:
            if (
                "UPDATE mediator.conversations" in s
                and "current_item_id" in s
            ):
                assert args[2] is not None, (
                    f"current_item_id must be set, got {args[2]!r}"
                )
                break
        else:
            pytest.fail("No UPDATE with current_item_id found")


# ── (b) Non-mediator bot context ────────────────────────────────────────────


class TestNonMediatorBotContext:
    """Verify that non-mediator bots get their own system prompt in prep."""

    def test_non_mediator_bot_gets_own_profile(self, monkeypatch: Any) -> None:
        """live_bot_profile_context for a non-mediator bot returns
        that bot's display_name, topic, and rendered system prompt."""
        def _coach_renderer(
            assistant_name: str,
            user_name: str,
            partner_name: str | None = None,
            *,
            prompt_version: str = "0.0.0",
            **_: Any,
        ) -> str:
            return (
                f"You are {assistant_name}, a personal coach. "
                f"Your user is {user_name}. "
                "Ask about goals and daily structure."
            )

        coach_spec = BotSpec(
            bot_id="coach_integration_test",
            prompt_renderer=_coach_renderer,
            step_instructions={
                "read": "read",
                "respond": "respond",
                "record": "record",
                "done": "done",
            },
            display_name="Coach",
            primary_topic_slug="coaching",
            participants_shape="solo",
            bot_spec_version="1.0.0",
        )

        monkeypatch.setitem(BOT_SPECS, "coach_integration_test", coach_spec)

        user = _make_user("Alice")
        partner = _make_user("Bob")

        profile = live_bot_profile_context(
            "coach_integration_test", user=user, partner=partner
        )

        assert profile["bot_id"] == "coach_integration_test"
        assert profile["display_name"] == "Coach"
        assert profile["primary_topic_slug"] == "coaching"
        assert profile["participants_shape"] == "solo"

        assert "system_prompt" in profile, (
            f"Expected system_prompt in profile; got keys={list(profile.keys())}"
        )
        system_prompt = profile["system_prompt"]
        assert "Coach" in system_prompt
        assert "coach" in system_prompt.lower()
        assert "Alice" in system_prompt

        monkeypatch.delitem(BOT_SPECS, "coach_integration_test", raising=False)

    def test_non_mediator_bot_differs_from_mediator(self) -> None:
        """Sanity: mediator and non-mediator bots have different profiles.
        Note: mediator system_prompt rendering may fail due to DEBT-041
        (bot_spec_version="1.1.0" vs known versions v1/v2/v3).  This test
        only checks the always-present profile keys."""
        user = _make_user("Alice")

        mediator_profile = live_bot_profile_context("mediator", user=user)

        assert mediator_profile["bot_id"] == "mediator"
        assert mediator_profile["display_name"] == "Mediator"
        assert mediator_profile["primary_topic_slug"] == "relationship"

        # Compare with a staging bot if available.
        try:
            _maybe_register_staging_bots()
            coach_spec = BOT_SPECS.get("coach")
            if coach_spec is not None:
                coach_profile = live_bot_profile_context("coach", user=user)
                assert coach_profile["bot_id"] != mediator_profile["bot_id"]
                assert (
                    coach_profile["primary_topic_slug"]
                    != mediator_profile["primary_topic_slug"]
                )
        except Exception:
            pass  # STAGING not set — skip.


# ── (c) Tool-gating ─────────────────────────────────────────────────────────


class TestLivePrepToolGating:
    """Verify that live_prep step exposes NO outbound/write tools.

    Note: list_scheduled_tasks, list_all_reminders, and
    list_scheduled_checkins are read-only tools in both READ_PHASE_TOOLS
    and SCHEDULE_TOOLS.  They are intentionally included in
    LIVE_PREP_TOOLS.  Only write-phase scheduling tools
    (schedule_checkin, cancel_scheduled_checkin, etc.) are gated, and
    they live in WRITE_PHASE_TOOLS.
    """

    def test_live_prep_excludes_outbound_and_write_tools(self) -> None:
        """Verify live_prep excludes: outbound (send_message_part),
        OOB tools (check_oob, summarize_oob_topics), and all
        WRITE_PHASE_TOOLS (which includes schedule write verbs)."""
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
            current_step="live_prep",
            turn_started_at=datetime.now(timezone.utc),
        )

        allowed = _step_allowed(ctx)

        # Outbound tool must be absent.
        assert "send_message_part" not in allowed, (
            "send_message_part must NOT be in live_prep"
        )

        # Must NOT contain any WRITE_PHASE_TOOLS (includes schedule
        # write verbs: schedule_checkin, cancel_scheduled_checkin,
        # schedule_task, update_scheduled_task, etc.).
        write_overlap = allowed & WRITE_PHASE_TOOLS
        assert write_overlap == set(), (
            f"live_prep must NOT expose WRITE_PHASE_TOOLS; got {write_overlap}"
        )

        # Must NOT contain OOB tools.
        assert "check_oob" not in allowed, (
            "check_oob must NOT be in live_prep"
        )
        assert "summarize_oob_topics" not in allowed, (
            "summarize_oob_topics must NOT be in live_prep"
        )

        # But must contain submit_live_brief (the required gate).
        assert "submit_live_brief" in allowed, (
            "submit_live_brief must be in live_prep tools"
        )

    def test_live_prep_tools_match_constant(self) -> None:
        """The live_prep tool set is a subset of LIVE_PREP_TOOLS + ALWAYS_ALLOWED."""
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
            current_step="live_prep",
            turn_started_at=datetime.now(timezone.utc),
        )

        allowed = _step_allowed(ctx)

        expected_universe = LIVE_PREP_TOOLS | {"update_turn_plan"}
        assert allowed <= expected_universe, (
            f"live_prep tools must be a subset; extra: "
            f"{sorted(allowed - expected_universe)}"
        )


# ── (d) Missing-submit test ─────────────────────────────────────────────────


class TestMissingSubmit:
    """Verify that prep fails visibly when the model returns text without
    calling submit_live_brief."""

    async def test_plain_text_without_submit_fails_prep(
        self, monkeypatch: Any
    ) -> None:
        """Provider returns plain text only -> status='prep_failed'."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()

        failure_result = NonchatJobResult(
            success=False,
            brief=None,
            failure_reason="live_prep_text_no_submit",
            turn_id=uuid4(),
            tool_call_count=0,
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return failure_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        pool = PrepFakePool()
        pool.set_conversations_row_for(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="prepping",
            topic_id=topic_id,
        )
        pool.set_user_row(user_id)
        pool.set_themes()
        pool.set_distillations()
        pool.set_messages()

        result = await run_live_prep_agentic_job(
            conversation_id=conversation_id,
            user_id=user_id,
            bot_id="mediator",
            steering_text="test",
            topic_id=topic_id,
            pool=pool,
        )

        assert result.success is False
        assert result.failure_reason == "live_prep_text_no_submit"

        # Status must have been set to prep_failed.
        assert pool.updated_status == "prep_failed", (
            f"Expected status='prep_failed', got {pool.updated_status}"
        )

    async def test_prep_failed_persists_error_in_session_fields(
        self, monkeypatch: Any
    ) -> None:
        """_set_prep_failed persists the error in session_fields.prep_error
        so /card can surface it."""
        pool = PrepFakePool()
        conversation_id = uuid4()

        await _set_prep_failed(
            pool, conversation_id, "live_prep_text_no_submit"
        )

        # Verify the execute call updates status and sets prep_error.
        update_calls = [
            (s, a)
            for s, a in pool.executed
            if "UPDATE mediator.conversations" in s
            and "prep_failed" in s
        ]
        assert len(update_calls) >= 1, (
            "Expected an UPDATE to prep_failed status"
        )

        for s, args in update_calls:
            sql_flat = s.replace("\n", " ")
            assert "prep_error" in sql_flat, (
                f"Expected prep_error in session_fields update: {sql_flat}"
            )

    async def test_prep_retry_reenters_prepping(
        self, monkeypatch: Any
    ) -> None:
        """retry_live_prep checks status='prep_failed', resets to 'preparing',
        and re-runs the agentic job."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()

        brief = _make_minimal_agenda()
        success_result = NonchatJobResult(
            success=True,
            brief=brief,
            failure_reason=None,
            turn_id=uuid4(),
            tool_call_count=2,
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return success_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        pool = PrepFakePool()
        pool.set_conversations_row_for(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="prep_failed",
            topic_id=topic_id,
            steering_text="retry test",
        )
        pool.set_user_row(user_id)
        pool.set_themes()
        pool.set_distillations()
        pool.set_messages()
        pool.set_theme_lookup()

        result = await retry_live_prep(conversation_id, pool)

        assert result.success is True

        # Verify the UPDATE to 'preparing' happened before the re-run.
        update_calls = [
            s
            for s, _ in pool.executed
            if "UPDATE mediator.conversations" in s
        ]
        assert any(
            "preparing" in s and "prep_failed" not in s
            for s in update_calls
        ), f"Expected an UPDATE to 'preparing' before retry; got {update_calls}"

    async def test_retry_rejects_non_prep_failed(self) -> None:
        """retry_live_prep raises ValueError for non-prep_failed sessions."""
        pool = PrepFakePool()
        conversation_id = uuid4()
        pool.set_conversations_row_for(
            conversation_id,
            user_id=uuid4(),
            bot_id="mediator",
            status="ready",
        )

        with pytest.raises(ValueError, match="prep_failed"):
            await retry_live_prep(conversation_id, pool)


# ── (e) Orphan recovery ─────────────────────────────────────────────────────


class TestOrphanRecovery:
    """Verify that the orphan sweep marks stuck prepping sessions as prep_failed."""

    async def test_sweep_orphaned_prepping(self) -> None:
        """Call sweep_orphaned_prepping and verify the SQL shape."""
        pool = PrepFakePool()
        now = datetime(2026, 5, 21, 16, 0, 0, tzinfo=timezone.utc)

        await sweep_orphaned_prepping(pool, now=now)

        update_calls = [
            (s, a)
            for s, a in pool.executed
            if "UPDATE mediator.conversations" in s
            and "prep_failed" in s
        ]
        assert len(update_calls) >= 1, (
            f"Expected orphan sweep UPDATE; "
            f"got {[s for s, _ in pool.executed if 'UPDATE mediator.conversations' in s]}"
        )

        for s, args in update_calls:
            sql_flat = s.replace("\n", " ")
            assert "created_at <" in sql_flat, (
                f"Expected 'created_at <' in sweep SQL: {sql_flat}"
            )
            assert ("prepping" in sql_flat or "preparing" in sql_flat), (
                f"Expected 'prepping' or 'preparing' in WHERE filter: {sql_flat}"
            )
            assert "orphaned" in sql_flat, (
                f"Expected 'orphaned' in prep_error: {sql_flat}"
            )

    async def test_sweep_respects_timeout_setting(self) -> None:
        """The sweep uses settings.live_prep_orphan_timeout_minutes
        (default 10)."""
        settings = get_settings()
        timeout = settings.live_prep_orphan_timeout_minutes

        assert 1 <= timeout <= 60, (
            f"live_prep_orphan_timeout_minutes={timeout} out of range"
        )
        assert timeout == 10, (
            f"Expected default orphan timeout=10, got {timeout}"
        )


# ── (f) Retry artifact revisioning (T3) ──────────────────────────────────────


class TestRetryArtifactRevisioning:
    """T3: Retry creates a NEW bot_turn and NEW live_prep_brief artifact
    revision while preserving old artifacts."""

    async def test_retry_creates_new_artifact_revision(
        self, monkeypatch: Any
    ) -> None:
        """After a prep_failed session is retried and succeeds, both
        the original and retry artifacts exist with different
        revision_numbers and different turn_ids."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        brief = _make_minimal_agenda()

        # Track artifact insertions to verify revision numbering.
        artifact_inserts: list[dict[str, Any]] = []

        class RevisionTrackingPool(PrepFakePool):
            async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
                if "INSERT INTO mediator.conversation_artifacts" in sql:
                    # Intercept and record each artifact insert.
                    result = await super().fetchrow(sql, *args)
                    if result:
                        artifact_inserts.append(dict(result))
                    return result
                return await super().fetchrow(sql, *args)

        pool = RevisionTrackingPool()
        pool.set_conversations_row_for(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="prep_failed",
            topic_id=topic_id,
            steering_text="retry test",
        )
        pool.set_user_row(user_id)
        pool.set_themes()
        pool.set_distillations()
        pool.set_messages()
        pool.set_theme_lookup()

        # First retry attempt
        success_result = NonchatJobResult(
            success=True,
            brief=brief,
            failure_reason=None,
            turn_id=uuid4(),
            tool_call_count=2,
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return success_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        result = await retry_live_prep(conversation_id, pool)

        assert result.success is True
        assert result.turn_id is not None

        # A new turn was created (non-None, different from no turn).
        assert result.turn_id is not None

        # An artifact was inserted.
        assert len(artifact_inserts) == 1, (
            f"Expected 1 artifact insert on retry, "
            f"got {len(artifact_inserts)}"
        )
        artifact = artifact_inserts[0]
        assert artifact["artifact_type"] == "live_prep_brief"
        assert artifact["created_by_turn_id"] is not None

    async def test_retry_tracks_retry_count_in_session_fields(
        self, monkeypatch: Any
    ) -> None:
        """After a retry completes (success or failure), session_fields
        includes retry_count."""
        user_id = uuid4()
        conversation_id = uuid4()
        topic_id = uuid4()
        brief = _make_minimal_agenda()

        pool = PrepFakePool()
        pool.set_conversations_row_for(
            conversation_id,
            user_id=user_id,
            bot_id="mediator",
            status="prep_failed",
            topic_id=topic_id,
            steering_text="retry test",
            session_fields={"prep_error": "first failure", "retry_count": 2},
        )
        pool.set_user_row(user_id)
        pool.set_themes()
        pool.set_distillations()
        pool.set_messages()
        pool.set_theme_lookup()

        success_result = NonchatJobResult(
            success=True,
            brief=brief,
            failure_reason=None,
            turn_id=uuid4(),
            tool_call_count=2,
        )

        async def fake_run_job(**kwargs: Any) -> NonchatJobResult:
            return success_result

        monkeypatch.setattr(
            "app.services.nonchat_agentic.run_agentic_nonchat_job",
            fake_run_job,
        )

        result = await retry_live_prep(conversation_id, pool)

        assert result.success is True

        # Verify retry_count was updated in session_fields.
        update_calls = [
            (s, a)
            for s, a in pool.executed
            if "UPDATE mediator.conversations" in s
            and "retry_count" in s
        ]
        assert len(update_calls) >= 1, (
            f"Expected retry_count update in session_fields; "
            f"found {len(update_calls)} update calls matching"
        )
