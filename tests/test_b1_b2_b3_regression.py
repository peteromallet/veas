"""Regression tests for B1/B2/B3 (Hector inbox stall, commit 3098853).

B1: claim CTE works against a message with next_retry_at already set.
B2: a turn ending in a pre-send failure leaves bot_turns.completed_at populated.
B3: a scheduling-intent inbound routes to the "standard" skeleton for Hector.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services import agentic
from app.services.inbound_queue import _claim_messages_for_turn_in_tx
from app.services.scope import InboundScope
from app.services.turn_plan import CHECKIN_CONFIRM_RE, pick_default_skeleton

pytestmark = pytest.mark.anyio


# ── helpers ──────────────────────────────────────────────────────────────────

_RELATIONSHIP_TOPIC = UUID("00000000-0000-4000-8000-000000000001")


# ── B1 ───────────────────────────────────────────────────────────────────────


async def test_claim_cte_clears_next_retry_at_without_trigger_raise(
    fake_pool,
) -> None:
    """Claiming a message with next_retry_at set must succeed.

    The claim CTE unconditionally sets ``next_retry_at = NULL`` (line 269 of
    inbound_queue.py).  In prod this triggers the migration-0046 writer-marker
    trigger which requires ``app.lifecycle_writer = 'inbound_queue'``.  Before
    B1 was fixed, agentic.py set it to ``'agentic'`` — the trigger raised and
    the whole transaction rolled back.

    FakePool does not enforce the writer-marker trigger.  This test validates
    the claim logic itself: a message with next_retry_at set is claimable and
    afterwards has next_retry_at = NULL.
    """
    fp = fake_pool
    u = uuid4()
    fp.users[u] = {"id": u, "name": "M", "phone": "1", "timezone": "UTC"}
    t = _RELATIONSHIP_TOPIC
    m = uuid4()
    now = datetime.now(UTC)

    # Seed a message that looks like it was failed+retried:
    # next_retry_at is in the past (eligible), failure_class is retryable.
    fp.messages[m] = {
        "id": m,
        "direction": "inbound",
        "sender_id": u,
        "content": "Help",
        "processing_state": "raw",
        "bot_id": "mediator",
        "topic_id": t,
        "sent_at": now - timedelta(minutes=2),
        "bot_turn_id": None,
        "processing_started_at": None,
        "processing_attempts": 1,
        "processing_error": None,
        "handling_result": "failed",
        "handled_by_turn_id": None,
        "handled_at": None,
        # These are the lifecycle columns that the claim CTE must NULL:
        "next_retry_at": now - timedelta(seconds=10),
        "failure_class": "retryable_pre_send",
    }

    INS = (
        "INSERT INTO bot_turns (triggered_by_message_id,triggering_message_ids,"
        "user_in_context,system_prompt_version,model_version,prompt_snapshot,"
        "prompt_snapshot_encrypted,bot_id,topic_id,bot_spec_version,"
        "hot_context_builder_version,tool_schema_version)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id"
    )
    ARGS = ("v1", "m", "s", "e", "mediator", t, "sv", "hc", "ts")

    async with fp.acquire() as c:
        async with c.transaction():
            # Use the corrected writer marker (was 'agentic' before B1 fix).
            await c.execute(
                "SELECT set_config('app.lifecycle_writer','inbound_queue',true)"
            )
            T1 = (await c.fetchrow(INS, m, [m], u, *ARGS))["id"]
            claimed = await _claim_messages_for_turn_in_tx(
                c, [m], bot_id="mediator", topic_id=t, new_bot_turn_id=T1
            )

    # Claim succeeded (B1 crash would have been an unhandled RaiseError here).
    assert claimed == [m]
    # Lifecycle columns were NULLed by the claim CTE.
    assert fp.messages[m]["next_retry_at"] is None
    assert fp.messages[m]["failure_class"] == "retryable_pre_send"  # NOT cleared
    assert fp.messages[m]["bot_turn_id"] == T1
    assert fp.messages[m]["processing_state"] == "processing"


# ── B2 ───────────────────────────────────────────────────────────────────────


async def test_pre_send_failure_sets_completed_at(fake_pool, app_env, monkeypatch):
    """A turn that fails before sending must leave completed_at populated.

    Before B2 was fixed, ``_fail_turn`` only stamped ``failure_reason`` and
    left ``completed_at = NULL``.  The turn became a zombie — invisible to
    crash-marking sweeps (which key on ``failure_reason IS NULL``) and to
    crashed-turn release (which requires ``failure_reason = 'crashed'``).

    This test triggers a ``RespondCapNoOutput`` failure (the B3 chain) and
    asserts ``completed_at`` is non-NULL afterward.
    """
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    fp = fake_pool
    fp.users[user.id] = {
        "id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone,
    }
    fp.users[partner.id] = {
        "id": partner.id, "name": partner.name, "phone": partner.phone, "timezone": partner.timezone,
    }
    message_id = uuid4()
    t = get_relationship_topic_id()
    fp.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "I need help",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "deleted_at": None,
        "whatsapp_message_id": "wa-1",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "bot_id": "mediator",
        "topic_id": t,
    }

    async def fake_run_step(
        client, ctx, system_prompt, hot_context_rendered, allowed_tools,
        seed_messages, **kwargs,
    ):
        # Simulate B3: respond step exhausts retries and raises RespondCapNoOutput.
        raise agentic.RespondCapNoOutput()

    monkeypatch.setattr(agentic, "run_step", fake_run_step)
    agentic.set_pool(fake_pool)

    scope = InboundScope(
        bot_id="mediator",
        transport="whatsapp",
        user_id=user.id,
        topic_id=t,
        channel_id=None,
        binding_id=uuid4(),
        dyad_id=uuid4(),
    )

    # The turn will fail.  The except handler now routes through
    # _finalize_turn_atomically which sets completed_at, then re-raises.
    with pytest.raises(Exception):
        await agentic.run_agentic_turn([message_id], user, scope=scope)

    # The turn must exist and be terminal.
    turns = list(fp.bot_turns.values())
    assert len(turns) >= 1
    turn = turns[0]
    assert turn["failure_reason"] is not None, (
        "Expected failure_reason to be set on the turn"
    )
    assert turn["completed_at"] is not None, (
        "B2 regression: completed_at must be set on a failed turn; "
        "was NULL before the _finalize_turn_atomically fix"
    )
    # Messages should be in 'failed' state (retryable).
    assert fp.messages[message_id]["processing_state"] == "failed"
    assert fp.messages[message_id]["failure_class"] == "retryable_pre_send"


# ── B3 ───────────────────────────────────────────────────────────────────────


class TestCheckinConfirmRegex:
    """CHECKIN_CONFIRM_RE matches scheduling-intent phrases."""

    @pytest.mark.parametrize("text,expected", [
        ("can you check in with me tomorrow?", True),
        ("please check in with me at 9pm", True),
        ("remind me to call my mom", True),
        ("schedule a check-in for Monday", True),
        ("schedule a reminder for next week", True),
        ("schedule checkin for tonight", True),
        ("set a reminder for 8am", True),
        ("send me a reminder at 3pm", True),
        ("give me a nudge tomorrow", True),
        ("give me a ping in 2 hours", True),
        # Negative cases — should NOT match
        ("I had a good workout today", False),
        ("what should I eat for breakfast", False),
        ("how are you doing", False),
        ("check it out", False),
        ("remind her about the appointment", False),  # "remind me", not "remind her"
        ("check in with her tomorrow", False),  # "with me", not "with her"
        ("set a follow up", False),  # "reminder", not "follow up"
    ])
    def test_checkin_confirm_re(self, text, expected):
        assert bool(CHECKIN_CONFIRM_RE.search(text)) == expected


def test_pick_default_skeleton_routes_checkin_to_standard_for_hector():
    """A scheduling-intent inbound for Hector must route to 'standard'.

    The ``quick_reply`` skeleton is ``["respond","done"]`` — no ``schedule``
    step.  Hector's prompt instructs him to call ``schedule_checkin`` when a
    user asks for a future check-in; calling it during ``respond`` would be
    rejected as ``step_not_allowed``.  ``pick_default_skeleton`` must detect
    the intent and return ``"standard"``.
    """
    text = "can you check in with me tomorrow at 9am?"
    metadata = {"kind": "inbound", "messages": [{"content": text}]}
    signals = {"bot_id": "hector"}

    result = pick_default_skeleton(
        trigger_metadata=metadata,
        charge="routine",
        hot_context_signals=signals,
    )
    assert result == "standard", (
        f"Expected 'standard' skeleton for check-in intent, got {result!r}"
    )


def test_pick_default_skeleton_quick_reply_for_non_scheduling_hector():
    """A routine non-scheduling Hector message stays on quick_reply."""
    text = "hello there how are you doing"
    metadata = {"kind": "inbound", "messages": [{"content": text}]}
    signals = {"bot_id": "hector"}

    result = pick_default_skeleton(
        trigger_metadata=metadata,
        charge="routine",
        hot_context_signals=signals,
    )
    assert result == "quick_reply", (
        f"Expected 'quick_reply' skeleton for non-scheduling message, got {result!r}"
    )


def test_pick_default_skeleton_checkin_does_not_affect_other_bots():
    """The check-in routing is Hector-specific; mediator stays on quick_reply."""
    text = "can you check in with me tomorrow?"
    metadata = {"kind": "inbound", "messages": [{"content": text}]}
    signals = {"bot_id": "mediator"}

    result = pick_default_skeleton(
        trigger_metadata=metadata,
        charge="routine",
        hot_context_signals=signals,
    )
    # Mediator does not have the Hector-specific check-in routing.
    assert result == "quick_reply", (
        f"Mediator should stay on quick_reply for check-in text, got {result!r}"
    )
