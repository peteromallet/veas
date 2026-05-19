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
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
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
    with pytest.raises(
        UnknownPromptVersion, match="unknown system prompt version: missing"
    ):
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
    assert (
        "If relevant positive context is already known, mention it gently" in rendered
    )
    assert "if not, ask one balancing question" in rendered
    assert "Do not force optimism, minimize hurt" in rendered
    assert "use positives to dilute a legitimate grievance" in rendered
    assert "are there moments they do make you feel loved?" in rendered


def test_prompt_includes_scheduling_judgment_and_relative_time_guidance() -> None:
    rendered = render_system_prompt("Mediator", "Maya", "Ben")

    assert "# Scheduling Judgment" in rendered
    assert "Use scheduling proactively" in rendered
    assert "Default to the scheduling tool's `delay` field" in rendered
    assert "simple duration requests" in rendered
    assert "now_local" in rendered
    assert "Never schedule in the past" in rendered


def test_prompt_no_longer_mounts_first_contact_section() -> None:
    rendered = render_system_prompt(
        "Mediator", "Maya", "Ben", prompt_version="v1", onboarding_state="pending"
    )

    assert "# First Contact" not in rendered
    assert "onboarding_state" not in rendered
    assert "Write the first message yourself using judgment" not in rendered
    assert "not a canned script" not in rendered


def test_prompt_omits_first_contact_when_onboarding_complete() -> None:
    rendered = render_system_prompt(
        "Mediator", "Maya", "Ben", prompt_version="v1", onboarding_state="welcomed"
    )

    assert "# First Contact" not in rendered
    assert "Write the first message yourself using judgment" not in rendered

    default_rendered = render_system_prompt(
        "Mediator", "Maya", "Ben", prompt_version="v1"
    )
    assert "# First Contact" not in default_rendered


def test_prompt_treats_garbled_voice_text_as_transcription_artifact() -> None:
    rendered = render_system_prompt("Mediator", "Maya", "Ben", prompt_version="v1")

    assert "# Voice Notes And Transcription Artifacts" in rendered
    assert "transcription artifact" in rendered
    assert "Do not over-interpret garbled wording" in rendered


def test_prompt_centers_revealing_followup_in_multi_message_bursts() -> None:
    rendered = render_system_prompt("Mediator", "Maya", "Ben", prompt_version="v1")

    assert "# Multi-Message Handling" in rendered
    assert "Let the follow-up become the center of gravity" in rendered
    assert "rather than a second mini-essay" in rendered
    assert "Avoid stacked responses" in rendered


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
    assert "close with one small helpful action" in rendered
    assert "Do not turn every ending into homework" in rendered
    assert "Keep action nudges small enough to do today or soon" in rendered
    assert "Prefer a closing sentence over another probing question" in rendered
    assert "Always leave the door open when closing" in rendered
    assert "unless you want to keep going" in rendered
    assert "Silence is also acceptable" in rendered
    assert "Goodnight" in rendered
    assert "schedule one in the schedule step rather than keeping the live chat open" in rendered


def test_v3_prompt_uses_adaptive_step_language() -> None:
    rendered = render_system_prompt(
        "Mediator",
        "Maya",
        "Ben",
        prompt_version="v3",
        current_user_sharing_default="unset",
    )

    assert "# Adaptive Turn Shape" in rendered
    assert "`record`: maintain durable state after the reply" in rendered
    assert "Phase B" not in rendered


def test_cross_thread_unset_branch_is_not_mounted_when_current_user_unset() -> None:
    rendered = render_system_prompt(
        "Mediator",
        "Maya",
        "Ben",
        prompt_version="v1",
        current_user_sharing_default="unset",
        partner_sharing_default="opt_in",
        current_user_partner_sharing_state="pending",
    )

    assert "Partner sharing is undecided" not in rendered
    assert "`set_partner_sharing(opt_in=true)`" not in rendered
    # opt-out soft-nudge content should not appear when user is unset
    assert "never pressure or repeat" not in rendered


def test_cross_thread_opt_out_branch_present_when_current_user_opt_out() -> None:
    rendered = render_system_prompt(
        "Mediator",
        "Maya",
        "Ben",
        prompt_version="v1",
        current_user_sharing_default="opt_out",
        partner_sharing_default="opt_in",
    )

    assert "private by default" in rendered
    assert "do not pressure or repeat the opt-in question" in rendered
    assert "gently surface the value sharing could unlock" not in rendered
    # urgent-ask phrasing should not appear when user has chosen opt_out
    assert "Treat this as urgent" not in rendered
    assert "ask them to choose `opt_in` or `opt_out`" not in rendered


def test_cross_thread_opt_in_branch_when_current_user_opt_in() -> None:
    rendered = render_system_prompt(
        "Mediator",
        "Maya",
        "Ben",
        prompt_version="v1",
        current_user_sharing_default="opt_in",
        partner_sharing_default="opt_in",
    )

    # Neither the urgent ask nor the soft nudge should appear when the user has chosen opt_in.
    assert "Treat this as urgent" not in rendered
    assert "ask them to choose `opt_in` or `opt_out`" not in rendered
    assert "never pressure or repeat" not in rendered
    assert "gently surface the value sharing could unlock" not in rendered
    assert "OOB always overrides opt-in" in rendered


def test_partner_perspective_active_when_partner_opt_in() -> None:
    rendered = render_system_prompt(
        "Mediator",
        "Maya",
        "Ben",
        prompt_version="v1",
        current_user_sharing_default="opt_in",
        partner_sharing_default="opt_in",
    )

    assert "# Surfacing The Partner's Perspective" in rendered
    assert "Keep their perspective live in this thread" in rendered
    assert "Be active, not passive" in rendered
    assert "Search before surfacing" in rendered


def test_partner_perspective_quiet_when_partner_opt_out() -> None:
    rendered = render_system_prompt(
        "Mediator",
        "Maya",
        "Ben",
        prompt_version="v1",
        current_user_sharing_default="opt_in",
        partner_sharing_default="opt_out",
    )

    assert "# Surfacing The Partner's Perspective" in rendered
    # Active surfacing block should not appear when partner is opt_out.
    assert "Keep their perspective live in this thread" not in rendered
    assert "Be active, not passive" not in rendered
    # The short reminder should appear instead.
    assert "Do not paraphrase partner-thread content" in rendered


async def test_eval_turn_uses_explicit_pool_prompt_version_and_fake_whatsapp(
    fake_pool, app_env, monkeypatch
) -> None:
    eval_pool = fake_pool
    global_pool = type(fake_pool)()
    user, _, message_id = _seed_pair(eval_pool)
    observed: dict[str, object] = {}

    async def oob_ok(
        pool, content, recipient_id, protected_owner_ids=None, *, bot_id, topic_id
    ):
        assert pool is eval_pool
        assert bot_id == "mediator"
        assert topic_id is not None
        return {"verdict": "ok", "reason": "test", "suggested_rewrite": None}

    async def fake_run_step(
        client,
        ctx,
        system_prompt,
        hot_context_rendered,
        allowed_tools,
        seed_messages,
        **kwargs,
    ):
        assert ctx.pool is eval_pool
        assert "Maya" in system_prompt
        assert "## Recent messages" in hot_context_rendered
        observed.setdefault("steps", []).append(ctx.current_step)
        if ctx.current_step == "respond":
            return "I hear you.", [{"role": "assistant", "content": "reply note"}], 0
        return "", [{"role": "assistant", "content": "step note"}], 0

    monkeypatch.setattr(hooks, "check_oob", oob_ok)
    monkeypatch.setattr(agentic, "run_step", fake_run_step)
    original_send_text = whatsapp.send_text
    agentic.set_pool(global_pool)

    try:
        result = await run_eval_turn(eval_pool, [message_id], user, prompt_version="v1")
    finally:
        agentic.set_pool(None)

    assert whatsapp.send_text is original_send_text
    assert observed["steps"] == ["read", "respond", "record", "schedule"]
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
    assert [
        (send.kind, send.to, send.payload, send.delivery_id)
        for send in result.whatsapp_sends
    ] == [("text", user.phone, "I hear you.", "eval-text-1")]


async def test_eval_turn_rejects_unknown_prompt_version_before_sending(
    fake_pool, app_env, monkeypatch
) -> None:
    user, _, message_id = _seed_pair(fake_pool)
    called = False

    async def fake_run_step(*args, **kwargs):
        nonlocal called
        called = True
        return "", [], 0

    monkeypatch.setattr(agentic, "run_step", fake_run_step)

    with pytest.raises(
        UnknownPromptVersion, match="unknown system prompt version: missing"
    ):
        await run_eval_turn(fake_pool, [message_id], user, prompt_version="missing")

    assert called is False
    assert fake_pool.bot_turns == {}
    assert [
        row for row in fake_pool.messages.values() if row["direction"] == "outbound"
    ] == []
