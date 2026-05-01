from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.user import User
from app.services import agentic

pytestmark = pytest.mark.anyio


def test_clean_user_facing_text_removes_internal_process_leaks():
    text = """The miscarriage mention is new — it's not in the stored memory yet. That's significant context. Responding now.

---

That parallel you just drew matters.

What do you think gets in the way?"""

    assert agentic.clean_user_facing_text(text) == (
        "That parallel you just drew matters.\n\n"
        "What do you think gets in the way?"
    )


def test_clean_user_facing_text_removes_broader_analysis_preamble():
    text = """The person's message is rich and self-aware — he's naming both his own patterns. No new tools needed; I have enough context.

---

That's a really honest thing to name.

The thing I'd gently push on: is the busyness doing some work for you?"""

    assert agentic.clean_user_facing_text(text) == (
        "That's a really honest thing to name.\n\n"
        "The thing I'd gently push on: is the busyness doing some work for you?"
    )


def test_clean_user_facing_text_removes_phase_error_leak():
    text = (
        "The system is still flagging my write calls as being in the read phase -- "
        "this appears to be a system constraint issue. My user-facing reply has "
        "already been delivered above. The watch item ed7ac62e should be addressed: "
        "The sender confirmed both flagged phrases were voice-to-text errors from a voice "
        "note, not descriptions of physical contact, and no safety escalation is "
        "warranted. The observation 4ccfee43 should be updated to reflect the same "
        "resolution."
    )

    assert agentic.clean_user_facing_text(text) == ""


def test_clean_user_facing_text_removes_write_tools_phase_gate_leak():
    text = (
        "It appears the write tools are returning phase errors unexpectedly. "
        "The user-facing reply has already been delivered. The key updates to "
        "record are: reinforcing the \"wither and die\" observation with the sender's "
        "clarification that love is still present, and updating the communication "
        "theme to reflect tonight's session depth. These will need to be retried "
        "when the phase gate resolves."
    )

    assert agentic.clean_user_facing_text(text) == ""


async def test_run_agentic_turn_lifecycle_ordering(fake_pool, app_env, monkeypatch):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    fake_pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    fake_pool.users[partner.id] = {
        "id": partner.id,
        "name": partner.name,
        "phone": partner.phone,
        "timezone": partner.timezone,
    }
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "I need help",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
        "charge": "charged",
        "deleted_at": None,
        "whatsapp_message_id": "wa-1",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
    }
    calls = []

    async def fake_run_phase(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages):
        calls.append((ctx.phase, seed_messages, fake_pool.messages[message_id]["processing_state"]))
        assert "## You" in hot_context_rendered
        if ctx.phase == "read":
            assert fake_pool.messages[message_id]["processing_state"] == "raw"
            return "I hear you.", [{"role": "assistant", "content": "reason note"}], 2
        assert seed_messages[-1]["content"].startswith("You sent: I hear you.")
        return "", [{"role": "assistant", "content": "write note"}], 3

    async def fake_send(pool, recipient, content, bot_turn_id=None, **kwargs):
        out_id = uuid4()
        pool.messages[out_id] = {
            "id": out_id,
            "direction": "outbound",
            "sender_id": None,
            "recipient_id": recipient.id,
            "content": content,
            "processing_state": "processed",
            "sent_at": datetime.now(UTC),
            "charge": None,
            "deleted_at": None,
        }
        return out_id

    monkeypatch.setattr(agentic, "run_phase", fake_run_phase)
    monkeypatch.setattr(agentic, "send_outbound", fake_send)
    agentic.set_pool(fake_pool)

    await agentic.run_agentic_turn([message_id], user)

    turn = next(iter(fake_pool.bot_turns.values()))
    assert "## You" in turn["prompt_snapshot"]
    assert "## Recent messages" in turn["prompt_snapshot"]
    assert fake_pool.messages[message_id]["processing_state"] == "processed"
    assert [call[0] for call in calls] == ["read", "write"]
    assert calls[0][2] == "raw"
    assert turn["tool_call_count"] == 5
    assert turn["final_output_message_id"] is not None
    assert turn["completed_at"] is not None
    assert "reason note" in turn["reasoning"]
    assert "write note" in turn["reasoning"]


async def test_run_agentic_records_outbound_before_phase_b(fake_pool, app_env, monkeypatch):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    fake_pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    fake_pool.users[partner.id] = {"id": partner.id, "name": partner.name, "phone": partner.phone, "timezone": partner.timezone}
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "I need help",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
        "charge": "charged",
        "deleted_at": None,
        "whatsapp_message_id": "wa-1",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
    }

    async def fake_run_phase(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages):
        if ctx.phase == "read":
            return "I hear you.", [], 0
        turn = next(iter(fake_pool.bot_turns.values()))
        assert turn["final_output_message_id"] is not None
        return "", [], 0

    async def fake_send(pool, recipient, content, bot_turn_id=None, **kwargs):
        out_id = uuid4()
        pool.messages[out_id] = {
            "id": out_id,
            "direction": "outbound",
            "sender_id": None,
            "recipient_id": recipient.id,
            "content": content,
            "processing_state": "processed",
            "sent_at": datetime.now(UTC),
            "charge": None,
            "deleted_at": None,
        }
        return out_id

    monkeypatch.setattr(agentic, "run_phase", fake_run_phase)
    monkeypatch.setattr(agentic, "send_outbound", fake_send)
    agentic.set_pool(fake_pool)

    await agentic.run_agentic_turn([message_id], user)


async def test_text_cap_defers_original_messages_and_sends_notice_once(fake_pool, app_env, monkeypatch):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    fake_pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    fake_pool.users[partner.id] = {"id": partner.id, "name": partner.name, "phone": partner.phone, "timezone": partner.timezone}
    message_id = uuid4()
    fake_pool.messages[message_id] = {
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
    }
    sent = []

    async def fake_run_phase(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages):
        raise agentic.SpendCapExceeded("cap")

    async def fake_send(pool, recipient, content, bot_turn_id=None, **kwargs):
        out_id = uuid4()
        sent.append(content)
        pool.messages[out_id] = {
            "id": out_id,
            "direction": "outbound",
            "sender_id": None,
            "recipient_id": recipient.id,
            "content": content,
            "processing_state": "processed",
            "sent_at": datetime.now(UTC),
            "charge": None,
            "deleted_at": None,
        }
        return out_id

    monkeypatch.setattr(agentic, "run_phase", fake_run_phase)
    monkeypatch.setattr(agentic, "send_outbound", fake_send)
    agentic.set_pool(fake_pool)

    await agentic.run_agentic_turn([message_id], user)
    await agentic.run_agentic_turn([message_id], user)

    assert fake_pool.messages[message_id]["processing_state"] == "deferred"
    assert sent == ["I'm running into limits today, will catch up tomorrow."]
    jobs = [job for job in fake_pool.scheduled_jobs.values() if job["job_type"] == "deferred_turn"]
    assert len(jobs) == 1
    turn = next(iter(fake_pool.bot_turns.values()))
    assert turn["completed_at"] is not None
    assert "spend cap" in turn["reasoning"].lower()
