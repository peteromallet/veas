from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.user import User
from app.services import agentic, hooks, whatsapp
from app.services.prompts import UnknownPromptVersion, render_system_prompt
from evals.execution import run_eval_turn

pytestmark = pytest.mark.anyio


def _seed_pair(fake_pool) -> tuple[User, User, object]:
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
        "content": "I need help saying this clearly.",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
        "charge": "charged",
        "deleted_at": None,
        "whatsapp_message_id": "wa-inbound",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
    }
    return user, partner, message_id


def test_known_prompt_version_renders_and_unknown_version_fails() -> None:
    rendered = render_system_prompt("Mediator", "Maya", "Ben", prompt_version="v1")

    assert "Mediator" in rendered
    assert "Maya" in rendered
    assert "Ben" in rendered
    with pytest.raises(UnknownPromptVersion, match="unknown system prompt version: missing"):
        render_system_prompt("Mediator", "Maya", "Ben", prompt_version="missing")


def test_prompt_includes_relational_voice_without_impersonation() -> None:
    rendered = render_system_prompt("Mediator", "Maya", "Ben", prompt_version="v1")

    assert "# Relational Voice" in rendered
    assert "hidden emotional logic beneath the surface argument" in rendered
    assert "Do not impersonate any real therapist" in rendered
    assert "what do you make of that?" in rendered


def test_prompt_pushes_balanced_perspective_without_forced_optimism() -> None:
    rendered = render_system_prompt("Mediator", "Maya", "Ben", prompt_version="v1")

    assert "surface contrary evidence and positive moments" in rendered
    assert "If relevant positive context is already known, mention it gently" in rendered
    assert "if not, ask one balancing question" in rendered
    assert "Do not force optimism, minimize hurt" in rendered
    assert "use positives to dilute a legitimate grievance" in rendered
    assert "are there moments they do make you feel loved?" in rendered


def test_prompt_tells_agent_to_handle_first_contact() -> None:
    rendered = render_system_prompt("Mediator", "Maya", "Ben", prompt_version="v1")

    assert "# First Contact" in rendered
    assert "onboarding_state" in rendered
    assert "Write the first message yourself using judgment" in rendered
    assert "not a canned script" in rendered


def test_prompt_treats_garbled_voice_text_as_transcription_artifact() -> None:
    rendered = render_system_prompt("Mediator", "Maya", "Ben", prompt_version="v1")

    assert "# Voice Notes And Transcription Artifacts" in rendered
    assert "transcription artifact" in rendered
    assert "Do not over-interpret garbled wording" in rendered


def test_prompt_pushes_real_world_conversation_and_action() -> None:
    rendered = render_system_prompt("Mediator", "Maya", "Ben", prompt_version="v1")

    assert "frequently, subtly, and sometimes forcefully nudge" in rendered
    assert "Ask whether they have actually discussed the issue" in rendered
    assert "when, where, how long, and what first sentence" in rendered
    assert "ordinary real-world things together" in rendered
    assert "bridge-builder, not the bridge" in rendered


def test_prompt_closes_low_energy_conversations() -> None:
    rendered = render_system_prompt("Mediator", "Maya", "Ben", prompt_version="v1")

    assert "# Conversation Closure" in rendered
    assert "naturally losing energy" in rendered
    assert "Prefer a closing sentence over another question" in rendered
    assert "Goodnight" in rendered
    assert "schedule one in Phase B rather than keeping the live chat open" in rendered


async def test_eval_turn_uses_explicit_pool_prompt_version_and_fake_whatsapp(fake_pool, app_env, monkeypatch) -> None:
    eval_pool = fake_pool
    global_pool = type(fake_pool)()
    user, _, message_id = _seed_pair(eval_pool)
    observed: dict[str, object] = {}

    async def oob_ok(pool, content, recipient_id):
        assert pool is eval_pool
        return {"verdict": "ok", "reason": "test", "suggested_rewrite": None}

    async def fake_run_phase(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages):
        assert ctx.pool is eval_pool
        assert "Maya" in system_prompt
        assert "## Recent messages" in hot_context_rendered
        observed.setdefault("phases", []).append(ctx.phase)
        if ctx.phase == "read":
            return "I hear you.", [{"role": "assistant", "content": "read note"}], 0
        return "", [{"role": "assistant", "content": "write note"}], 0

    monkeypatch.setattr(hooks, "check_oob", oob_ok)
    monkeypatch.setattr(agentic, "run_phase", fake_run_phase)
    original_send_text = whatsapp.send_text
    agentic.set_pool(global_pool)

    try:
        result = await run_eval_turn(eval_pool, [message_id], user, prompt_version="v1")
    finally:
        agentic.set_pool(None)

    assert whatsapp.send_text is original_send_text
    assert observed["phases"] == ["read", "write"]
    assert global_pool.bot_turns == {}
    assert len(eval_pool.bot_turns) == 1
    turn = next(iter(eval_pool.bot_turns.values()))
    assert turn["system_prompt_version"] == "v1"
    assert "## Recent messages" in turn["prompt_snapshot"]
    outbound_id = turn["final_output_message_id"]
    assert eval_pool.messages[outbound_id]["whatsapp_message_id"] == "eval-text-1"
    assert eval_pool.messages[outbound_id]["processing_state"] == "processed"
    assert eval_pool.messages[message_id]["processing_state"] == "processed"
    assert result.tool_calls == []
    assert [(send.kind, send.to, send.payload, send.delivery_id) for send in result.whatsapp_sends] == [
        ("text", user.phone, "I hear you.", "eval-text-1")
    ]


async def test_eval_turn_rejects_unknown_prompt_version_before_sending(fake_pool, app_env, monkeypatch) -> None:
    user, _, message_id = _seed_pair(fake_pool)
    called = False

    async def fake_run_phase(*args, **kwargs):
        nonlocal called
        called = True
        return "", [], 0

    monkeypatch.setattr(agentic, "run_phase", fake_run_phase)

    with pytest.raises(UnknownPromptVersion, match="unknown system prompt version: missing"):
        await run_eval_turn(fake_pool, [message_id], user, prompt_version="missing")

    assert called is False
    assert fake_pool.bot_turns == {}
    assert [row for row in fake_pool.messages.values() if row["direction"] == "outbound"] == []
