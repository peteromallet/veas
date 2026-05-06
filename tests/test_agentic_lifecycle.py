from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.user import User
from app.config import get_settings
from app.services import agentic
from app.services.tools.registry import call_tool
from app.services.pacer import PacingDecision

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


def test_clean_user_facing_text_removes_interrupted_process_leak():
    text = "Interrupted — I'll pick up from Peter's next message."

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

    async def fake_run_step(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages, **kwargs):
        calls.append((ctx.current_step, seed_messages, fake_pool.messages[message_id]["processing_state"], ctx.trigger_metadata))
        assert "## You" in hot_context_rendered
        if ctx.current_step == "read":
            assert fake_pool.messages[message_id]["processing_state"] == "raw"
            return "reason note", [{"role": "assistant", "content": "reason note"}], 2
        if ctx.current_step == "respond":
            return "I hear you.", [{"role": "assistant", "content": "reply note"}], 0
        if ctx.current_step == "record":
            assert seed_messages[-1]["content"].startswith("Next step: record.")
            assert "You sent: I hear you." in seed_messages[-1]["content"]
            return "write note", [{"role": "assistant", "content": "write note"}], 3
        return "", [{"role": "assistant", "content": "schedule note"}], 0

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

    monkeypatch.setattr(agentic, "run_step", fake_run_step)
    monkeypatch.setattr(agentic, "send_outbound", fake_send)
    agentic.set_pool(fake_pool)

    await agentic.run_agentic_turn([message_id], user)

    turn = next(iter(fake_pool.bot_turns.values()))
    assert "## You" in turn["prompt_snapshot"]
    assert "## Recent messages" in turn["prompt_snapshot"]
    assert fake_pool.messages[message_id]["processing_state"] == "processed"
    assert [call[0] for call in calls] == ["read", "respond", "record", "schedule"]
    assert calls[0][2] == "raw"
    assert [call[3].get("kind", "inbound") for call in calls] == ["inbound"] * 4
    assert all("job_id" not in call[3].get("context", {}) for call in calls)
    assert turn["tool_call_count"] == 5
    assert turn["final_output_message_id"] is not None
    assert turn["completed_at"] is not None
    assert "reason note" in turn["reasoning"]
    assert "write note" in turn["reasoning"]


async def test_run_agentic_turn_with_metadata_seeds_compact_pacing_context(fake_pool, app_env, monkeypatch):
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
        "content": "That was a lot, but I think I am done now",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "deleted_at": None,
        "whatsapp_message_id": "wa-pace",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
    }
    decision = PacingDecision(
        action="answer",
        reason="burst settled after short wait",
        signal_snapshot={
            "source": "live",
            "message_count": 3,
            "typing_active": False,
            "latest_message_age_s": 2.1,
            "contains_question": False,
            "irrelevant_large_blob": "x" * 400,
        },
        preference_snapshot={"conversation_pace": "standard", "allow_reactions": True, "ignored": "value"},
    )
    seed_contents = []

    async def fake_run_step(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages, **kwargs):
        if ctx.current_step == "respond":
            seed_contents.append(seed_messages[-1]["content"])
            assert "pacing" in hot_context_rendered
            assert "burst settled after short wait" in hot_context_rendered
            return "I hear the whole thought.", [], 0
        return "", [], 0

    async def fake_send(pool, recipient, content, bot_turn_id=None, **kwargs):
        assert kwargs["protected_owner_ids"] == [user.id, partner.id]
        assert kwargs["send_typing_indicator"] is False
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

    monkeypatch.setattr(agentic, "run_step", fake_run_step)
    monkeypatch.setattr(agentic, "send_outbound", fake_send)
    agentic.set_pool(fake_pool)

    await agentic.run_agentic_turn_with_metadata([message_id], user, pacing_context=decision)

    assert seed_contents
    assert '"pacing"' in seed_contents[0]
    assert '"action": "answer"' in seed_contents[0]
    assert '"source": "live"' in seed_contents[0]
    assert "irrelevant_large_blob" not in seed_contents[0]
    turn = next(iter(fake_pool.bot_turns.values()))
    assert "pacing" in turn["prompt_snapshot"]
    assert "I hear the whole thought." == fake_pool.messages[turn["final_output_message_id"]]["content"]
    assert fake_pool.messages[message_id]["processing_state"] == "processed"


async def test_run_agentic_job_propagates_scheduled_task_trigger_metadata(fake_pool, app_env, monkeypatch):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    fake_pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    fake_pool.users[partner.id] = {
        "id": partner.id,
        "name": partner.name,
        "phone": partner.phone,
        "timezone": partner.timezone,
    }
    job_id = uuid4()
    task_id = uuid4()
    trigger_metadata = {
        "kind": "scheduled_task",
        "context": {
            "job_id": str(job_id),
            "task_id": str(task_id),
            "brief": "Draft tomorrow's repair prompt",
            "recurrence": {"type": "daily", "interval": 1},
        },
    }
    seen = []

    async def fake_run_step(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages, **kwargs):
        seen.append((ctx.current_step, ctx.triggering_message_ids, ctx.trigger_metadata))
        assert "scheduled_task" in hot_context_rendered
        return "", [], 0

    monkeypatch.setattr(agentic, "run_step", fake_run_step)

    await agentic.run_agentic_job_with_pool(
        fake_pool,
        user,
        trigger_metadata,
        prompt_version="v1",
    )

    assert [phase for phase, _, _ in seen] == ["read", "respond", "record", "schedule"]
    assert all(message_ids == [] for _, message_ids, _ in seen)
    assert all(metadata["kind"] == "scheduled_task" for _, _, metadata in seen)
    assert all(metadata["context"]["job_id"] == str(job_id) for _, _, metadata in seen)


async def test_run_agentic_records_outbound_before_record_step(fake_pool, app_env, monkeypatch):
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

    async def fake_run_step(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages, **kwargs):
        if ctx.current_step == "respond":
            return "I hear you.", [], 0
        if ctx.current_step == "record":
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

    monkeypatch.setattr(agentic, "run_step", fake_run_step)
    monkeypatch.setattr(agentic, "send_outbound", fake_send)
    agentic.set_pool(fake_pool)

    await agentic.run_agentic_turn([message_id], user)


async def test_run_agentic_skips_final_reply_when_newer_inbound_arrives(fake_pool, app_env, monkeypatch):
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
        "content": "First part",
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

    async def fake_run_step(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages, **kwargs):
        if ctx.current_step == "respond":
            newer_id = uuid4()
            fake_pool.messages[newer_id] = {
                "id": newer_id,
                "direction": "inbound",
                "sender_id": user.id,
                "recipient_id": None,
                "content": "Second part",
                "processing_state": "raw",
                "sent_at": ctx.turn_started_at + timedelta(milliseconds=1),
                "charge": "routine",
                "deleted_at": None,
                "whatsapp_message_id": "wa-2",
                "media_type": None,
                "media_url": None,
                "media_duration_seconds": None,
                "media_analysis": None,
                "edit_history": None,
                "edited_at": None,
            }
            return "I was mid-reply.", [], 0
        return "", [], 0

    async def fake_send(*args, **kwargs):
        sent.append(args)
        return uuid4()

    monkeypatch.setattr(agentic, "run_step", fake_run_step)
    monkeypatch.setattr(agentic, "send_outbound", fake_send)
    agentic.set_pool(fake_pool)

    await agentic.run_agentic_turn([message_id], user)

    turn = next(iter(fake_pool.bot_turns.values()))
    assert sent == []
    assert turn["final_output_message_id"] is None
    assert "Final outbound skipped because a newer inbound message arrived before send." in turn["reasoning"]


async def test_run_agentic_skips_final_reply_when_newer_inbound_arrives_before_turn_opens(
    fake_pool, app_env, monkeypatch
):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    fake_pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    fake_pool.users[partner.id] = {"id": partner.id, "name": partner.name, "phone": partner.phone, "timezone": partner.timezone}
    message_id = uuid4()
    first_sent_at = datetime.now(UTC) - timedelta(seconds=5)
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "wait nvm",
        "processing_state": "raw",
        "sent_at": first_sent_at,
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

    async def fake_run_step(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages, **kwargs):
        if ctx.current_step == "respond":
            newer_id = uuid4()
            newer_sent_at = first_sent_at + timedelta(seconds=1)
            assert newer_sent_at < ctx.turn_started_at
            fake_pool.messages[newer_id] = {
                "id": newer_id,
                "direction": "inbound",
                "sender_id": user.id,
                "recipient_id": None,
                "content": "wait one more",
                "processing_state": "raw",
                "sent_at": newer_sent_at,
                "charge": "routine",
                "deleted_at": None,
                "whatsapp_message_id": "wa-2",
                "media_type": None,
                "media_url": None,
                "media_duration_seconds": None,
                "media_analysis": None,
                "edit_history": None,
                "edited_at": None,
            }
            return "Still here. What was it?", [], 0
        return "", [], 0

    async def fake_send(*args, **kwargs):
        sent.append(args)
        return uuid4()

    monkeypatch.setattr(agentic, "run_step", fake_run_step)
    monkeypatch.setattr(agentic, "send_outbound", fake_send)
    agentic.set_pool(fake_pool)

    await agentic.run_agentic_turn([message_id], user)

    turn = next(iter(fake_pool.bot_turns.values()))
    assert sent == []
    assert turn["final_output_message_id"] is None
    assert "Final outbound skipped because a newer inbound message arrived before send." in turn["reasoning"]


async def test_run_agentic_send_message_part_is_visible_to_record_step(fake_pool, app_env, monkeypatch):
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_MULTI_MESSAGE_DELAY_S", "0")
    get_settings.cache_clear()
    user = User(uuid4(), "Maya", "456", "UTC")
    partner = User(uuid4(), "Ben", "789", "UTC")
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
        "onboarding_state": "pending",
    }
    fake_pool.users[partner.id] = {
        "id": partner.id,
        "name": partner.name,
        "phone": partner.phone,
        "timezone": partner.timezone,
        "onboarding_state": "pending",
    }
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "I don't know",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
        "charge": "charged",
        "deleted_at": None,
        "whatsapp_message_id": "discord-in-3",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
    }
    sent = []
    record_seed = []
    schedule_seed = []

    async def fake_discord_send(to, body, *, send_typing_indicator=True):
        sent.append((to, body, send_typing_indicator))
        return {"messages": [{"id": f"discord-out-{len(sent)}"}]}

    async def fake_run_step(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages, **kwargs):
        if ctx.current_step == "respond":
            assert "send_message_part" in allowed_tools
            assert "explicit multi-message requests" in seed_messages[-1]["content"]
            first = await call_tool(
                "send_message_part",
                {"content": "That sounds bleak."},
                ctx,
            )
            assert first["status"] == "sent"
            assert first["sent_so_far"] == ["That sounds bleak."]
            second = await call_tool(
                "send_message_part",
                {"content": "What feels most impossible about it tonight?"},
                ctx,
            )
            assert second["status"] == "sent"
            assert second["sent_so_far"] == [
                "That sounds bleak.",
                "What feels most impossible about it tonight?",
            ]
            return "", [{"role": "assistant", "content": "used incremental sends"}], 2
        if ctx.current_step == "record":
            record_seed.append(seed_messages[-1]["content"])
        if ctx.current_step == "schedule":
            schedule_seed.append(seed_messages[-1]["content"])
        return "", [], 0

    monkeypatch.setattr(agentic, "run_step", fake_run_step)
    monkeypatch.setattr("app.services.discord.send_text", fake_discord_send)
    agentic.set_pool(fake_pool)

    await agentic.run_agentic_turn([message_id], user)

    turn = next(iter(fake_pool.bot_turns.values()))
    assert sent == [
        ("456", "That sounds bleak.", True),
        ("456", "What feels most impossible about it tonight?", True),
    ]
    assert turn["final_output_message_id"] is not None
    assert fake_pool.messages[turn["final_output_message_id"]]["content"] == (
        "What feels most impossible about it tonight?"
    )
    assert "You actually sent 2 messages" in record_seed[0]
    assert "1. That sounds bleak." in record_seed[0]
    assert "2. What feels most impossible about it tonight?" in record_seed[0]
    assert "You actually sent 2 messages" in schedule_seed[0]
    assert "1. That sounds bleak." in schedule_seed[0]
    assert "2. What feels most impossible about it tonight?" in schedule_seed[0]
    assert fake_pool.users[user.id]["onboarding_state"] == "welcomed"
    get_settings.cache_clear()


async def test_run_agentic_can_react_instead_of_replying(fake_pool, app_env, monkeypatch):
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    from app.config import get_settings

    get_settings.cache_clear()
    user = User(uuid4(), "Maya", "456", "UTC")
    partner = User(uuid4(), "Ben", "789", "UTC")
    fake_pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    fake_pool.users[partner.id] = {"id": partner.id, "name": partner.name, "phone": partner.phone, "timezone": partner.timezone}
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "Goodnight",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "deleted_at": None,
        "whatsapp_message_id": "discord-in-1",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
    }
    reactions = []

    async def fake_run_step(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages, **kwargs):
        if ctx.current_step == "respond":
            assert "search_emojis" in seed_messages[-1]["content"]
            assert "[react: emoji]" in seed_messages[-1]["content"]
            assert "do not claim Discord reactions are unavailable" in seed_messages[-1]["content"]
            return "[react: 👋]", [{"role": "assistant", "content": "[react: 👋]"}], 0
        if ctx.current_step == "record":
            assert "[reaction 👋]" in seed_messages[-1]["content"]
        return "", [], 0

    async def fake_add_reaction(phone, discord_message_id, emoji):
        reactions.append((phone, discord_message_id, emoji))

    monkeypatch.setattr(agentic, "run_step", fake_run_step)
    monkeypatch.setattr(agentic.discord, "add_reaction", fake_add_reaction)
    agentic.set_pool(fake_pool)

    await agentic.run_agentic_turn([message_id], user)

    turn = next(iter(fake_pool.bot_turns.values()))
    assert reactions == [("456", "discord-in-1", "👋")]
    assert turn["final_output_message_id"] is None
    assert "Reacted to triggering message with 👋" in turn["reasoning"]
    assert fake_pool.messages[message_id]["processing_state"] == "processed"
    get_settings.cache_clear()


async def test_run_agentic_can_react_alongside_reply(fake_pool, app_env, monkeypatch):
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    from app.config import get_settings

    get_settings.cache_clear()
    user = User(uuid4(), "Maya", "456", "UTC")
    partner = User(uuid4(), "Ben", "789", "UTC")
    fake_pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    fake_pool.users[partner.id] = {"id": partner.id, "name": partner.name, "phone": partner.phone, "timezone": partner.timezone}
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "I did it",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "deleted_at": None,
        "whatsapp_message_id": "discord-in-2",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
    }
    reactions = []
    sent = []

    async def fake_run_step(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages, **kwargs):
        if ctx.current_step == "respond":
            return "[react: ❤️]\nThat matters. Let it land for a bit.", [], 0
        if ctx.current_step == "record":
            assert "That matters. Let it land for a bit." in seed_messages[-1]["content"]
        return "", [], 0

    async def fake_add_reaction(phone, discord_message_id, emoji):
        reactions.append((phone, discord_message_id, emoji))

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

    monkeypatch.setattr(agentic, "run_step", fake_run_step)
    monkeypatch.setattr(agentic.discord, "add_reaction", fake_add_reaction)
    monkeypatch.setattr(agentic, "send_outbound", fake_send)
    agentic.set_pool(fake_pool)

    await agentic.run_agentic_turn([message_id], user)

    turn = next(iter(fake_pool.bot_turns.values()))
    assert reactions == [("456", "discord-in-2", "❤️")]
    assert sent == ["That matters. Let it land for a bit."]
    assert turn["final_output_message_id"] is not None
    assert "[react:" not in fake_pool.messages[turn["final_output_message_id"]]["content"]
    get_settings.cache_clear()


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

    async def fake_run_step(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages, **kwargs):
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

    monkeypatch.setattr(agentic, "run_step", fake_run_step)
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
