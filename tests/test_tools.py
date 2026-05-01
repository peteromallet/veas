from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.models.user import User
from app.services.turn_context import TurnContext
from app.services.scheduled_job_handlers import schedule_checkin_job
from app.services.tools import read_tools, write_tools
from app.services.tools.registry import call_tool
from tool_schemas import (
    AddMemoryInput,
    AddOOBInput,
    AddWatchItemInput,
    AddressWatchItemInput,
    CancelScheduledCheckinInput,
    CheckOOBInput,
    Confidence,
    CreateThemeInput,
    EscalateToPartnerInput,
    FeedbackSentiment,
    GetOOBInput,
    LiftOOBInput,
    LogFeedbackInput,
    LogObservationInput,
    OOBSeverity,
    RecentActivityInput,
    ScheduleCheckinInput,
    SupersedeMemoryInput,
    ThemeHealth,
    ThemeSentiment,
    UpdateMemoryInput,
    UpdateOOBInput,
    UpdateObservationInput,
    UpdateThemeInput,
    UpdateUserStyleNotesInput,
    UpdateWatchItemInput,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def tool_ctx(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    fake_pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    fake_pool.users[partner.id] = {"id": partner.id, "name": partner.name, "phone": partner.phone, "timezone": partner.timezone}
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {"id": turn_id, "reasoning": "", "completed_at": None, "failure_reason": None}
    return TurnContext(turn_id, fake_pool, user, partner, [uuid4()], phase="write")


def _seed_memory(pool, about_user_id):
    memory_id = uuid4()
    pool.memories[memory_id] = {
        "id": memory_id,
        "about_user_id": about_user_id,
        "content": "old",
        "related_theme_ids": [],
        "status": "active",
        "created_at": datetime.now(UTC),
        "last_referenced_at": None,
    }
    return memory_id


def _seed_theme(pool):
    theme_id = uuid4()
    pool.themes[theme_id] = {
        "id": theme_id,
        "title": "Theme",
        "description": "desc",
        "status": "active",
        "sentiment": "mixed",
        "health": "tender",
        "last_reinforced_at": None,
        "last_active_at": datetime.now(UTC),
    }
    return theme_id


def _seed_watch(pool, user_id):
    watch_id = uuid4()
    pool.watch_items[watch_id] = {
        "id": watch_id,
        "owner_user_id": user_id,
        "content": "watch",
        "status": "open",
        "related_theme_ids": [],
        "due_at": None,
    }
    return watch_id


def _seed_observation(pool, user_id):
    observation_id = uuid4()
    pool.observations[observation_id] = {
        "id": observation_id,
        "about_user_id": user_id,
        "content": "obs",
        "confidence": "medium",
        "significance": 3,
        "related_theme_ids": [],
        "status": "active",
    }
    return observation_id


def _seed_oob(pool, user_id):
    oob_id = uuid4()
    pool.out_of_bounds[oob_id] = {
        "id": oob_id,
        "owner_id": user_id,
        "sensitive_core": "private",
        "shareable_context": None,
        "severity": "firm",
        "status": "active",
    }
    return oob_id


def _seed_job(pool, user_id):
    job_id = uuid4()
    pool.scheduled_jobs[job_id] = {
        "id": job_id,
        "user_id": user_id,
        "job_type": "checkin",
        "scheduled_for": datetime.now(UTC) + timedelta(hours=1),
        "context": {},
        "status": "pending",
    }
    return job_id


@pytest.mark.parametrize(
    ("tool_name", "call_factory"),
    [
        ("update_user_style_notes", lambda ctx: (write_tools.update_user_style_notes, UpdateUserStyleNotesInput(user_id=ctx.user.id, notes="short"))),
        ("add_memory", lambda ctx: (write_tools.add_memory, AddMemoryInput(about_user_id=ctx.user.id, content="memory"))),
        ("update_memory", lambda ctx: (write_tools.update_memory, UpdateMemoryInput(memory_id=_seed_memory(ctx.pool, ctx.user.id), content="new"))),
        ("supersede_memory", lambda ctx: (write_tools.supersede_memory, SupersedeMemoryInput(old_memory_id=_seed_memory(ctx.pool, ctx.user.id), new_content="new"))),
        ("create_theme", lambda ctx: (write_tools.create_theme, CreateThemeInput(title="Theme", description="desc", sentiment=ThemeSentiment.mixed, health=ThemeHealth.tender))),
        ("update_theme", lambda ctx: (write_tools.update_theme, UpdateThemeInput(theme_id=_seed_theme(ctx.pool), mark_reinforced=True))),
        ("add_watch_item", lambda ctx: (write_tools.add_watch_item, AddWatchItemInput(owner_user_id=ctx.user.id, content="watch"))),
        ("update_watch_item", lambda ctx: (write_tools.update_watch_item, UpdateWatchItemInput(watch_item_id=_seed_watch(ctx.pool, ctx.user.id), content="new"))),
        ("address_watch_item", lambda ctx: (write_tools.address_watch_item, AddressWatchItemInput(watch_item_id=_seed_watch(ctx.pool, ctx.user.id), addressing_note="handled"))),
        ("log_observation", lambda ctx: (write_tools.log_observation, LogObservationInput(content="obs", about_user_id=ctx.user.id, confidence=Confidence.medium, significance=3))),
        ("update_observation", lambda ctx: (write_tools.update_observation, UpdateObservationInput(observation_id=_seed_observation(ctx.pool, ctx.user.id), content="new"))),
        ("add_oob", lambda ctx: (write_tools.add_oob, AddOOBInput(owner_id=ctx.user.id, sensitive_core="private", severity=OOBSeverity.firm))),
        ("update_oob", lambda ctx: (write_tools.update_oob, UpdateOOBInput(oob_id=_seed_oob(ctx.pool, ctx.user.id), sensitive_core="new"))),
        ("lift_oob", lambda ctx: (write_tools.lift_oob, LiftOOBInput(oob_id=_seed_oob(ctx.pool, ctx.user.id)))),
        ("schedule_checkin", lambda ctx: (write_tools.schedule_checkin, ScheduleCheckinInput(user_id=ctx.user.id, when=datetime.now(UTC) + timedelta(hours=2), about_what="talk", reason="follow up"))),
        ("cancel_scheduled_checkin", lambda ctx: (_seed_job(ctx.pool, ctx.user.id) and write_tools.cancel_scheduled_checkin, CancelScheduledCheckinInput(user_id=ctx.user.id))),
        ("escalate_to_partner", lambda ctx: (write_tools.escalate_to_partner, EscalateToPartnerInput(from_user_id=ctx.user.id, to_user_id=ctx.partner.id, content="body", reason="crisis charge", is_crisis=True))),
        ("log_feedback", lambda ctx: (write_tools.log_feedback, LogFeedbackInput(from_user_id=ctx.user.id, target_type="general", target_id=None, sentiment=FeedbackSentiment.positive, content="good"))),
    ],
)
async def test_every_write_tool_inserts_tool_call(tool_ctx, monkeypatch, tool_name, call_factory):
    sent = []

    async def fake_send(pool, recipient, content, template_fallback=None, bot_turn_id=None, protected_owner_ids=None):
        sent.append((recipient, content, template_fallback, bot_turn_id, protected_owner_ids))
        return uuid4()

    monkeypatch.setattr(write_tools, "send_outbound", fake_send)
    fn, args = call_factory(tool_ctx)
    if tool_name == "escalate_to_partner":
        tool_ctx.trigger_charge = "crisis"

    await fn(tool_ctx, args)

    row = tool_ctx.pool.tool_calls[-1]
    assert row["turn_id"] == tool_ctx.turn_id
    assert row["tool_name"] == tool_name
    assert isinstance(row["arguments"], dict)
    assert isinstance(row["result"], dict)
    assert row["duration_ms"] is not None


async def test_supersede_memory_flips_old_and_links_new(tool_ctx):
    old_id = _seed_memory(tool_ctx.pool, tool_ctx.user.id)

    result = await write_tools.supersede_memory(
        tool_ctx,
        SupersedeMemoryInput(old_memory_id=old_id, new_content="replacement"),
    )

    assert tool_ctx.pool.memories[old_id]["status"] == "superseded"
    assert tool_ctx.pool.memories[result.new_id]["supersedes_memory_id"] == old_id


async def test_schedule_checkin_supersedes_prior_pending(tool_ctx):
    old_id = _seed_job(tool_ctx.pool, tool_ctx.user.id)

    result = await write_tools.schedule_checkin(
        tool_ctx,
        ScheduleCheckinInput(
            user_id=tool_ctx.user.id,
            when=datetime.now(UTC) + timedelta(hours=3),
            about_what="the repair",
            reason="worth a check-in",
        ),
    )

    assert result.superseded_job_id == old_id
    assert tool_ctx.pool.scheduled_jobs[old_id]["status"] == "superseded"
    assert tool_ctx.pool.scheduled_jobs[result.job_id]["status"] == "pending"
    assert tool_ctx.pool.scheduled_jobs[result.job_id]["scheduled_for"].tzinfo == UTC


async def test_schedule_checkin_rejects_naive_datetime(tool_ctx):
    with pytest.raises(ValueError):
        ScheduleCheckinInput(
            user_id=tool_ctx.user.id,
            when=datetime.now(),
            about_what="naive",
            reason="should fail",
        )

    with pytest.raises(ValueError):
        await schedule_checkin_job(
            tool_ctx.pool,
            tool_ctx.user.id,
            scheduled_for=datetime.now(),
            context={"about_what": "naive"},
        )


async def test_schedule_checkin_job_shares_utc_supersede_path(tool_ctx):
    old_id = _seed_job(tool_ctx.pool, tool_ctx.user.id)
    plus_two = timezone(timedelta(hours=2))

    row = await schedule_checkin_job(
        tool_ctx.pool,
        tool_ctx.user.id,
        scheduled_for=datetime(2026, 5, 1, 9, 30, tzinfo=plus_two),
        context={"about_what": "handler path", "reason": "shared helper"},
    )

    assert tool_ctx.pool.scheduled_jobs[old_id]["status"] == "superseded"
    assert tool_ctx.pool.scheduled_jobs[row["job_id"]]["scheduled_for"] == datetime(2026, 5, 1, 7, 30, tzinfo=UTC)


async def test_watch_item_due_schedules_due_job(tool_ctx):
    due_at = datetime.now(UTC) + timedelta(days=2)

    result = await write_tools.add_watch_item(
        tool_ctx,
        AddWatchItemInput(owner_user_id=tool_ctx.user.id, content="check after the talk", due_at=due_at),
    )

    jobs = [job for job in tool_ctx.pool.scheduled_jobs.values() if job["job_type"] == "watch_item_due"]
    assert len(jobs) == 1
    assert jobs[0]["user_id"] == tool_ctx.user.id
    assert jobs[0]["scheduled_for"] == due_at
    assert jobs[0]["context"]["watch_item_id"] == str(result.id)


async def test_oob_review_at_schedules_review_job(tool_ctx):
    review_at = datetime.now(UTC) + timedelta(days=14)

    result = await write_tools.add_oob(
        tool_ctx,
        AddOOBInput(
            owner_id=tool_ctx.user.id,
            sensitive_core="private family detail",
            severity=OOBSeverity.firm,
            review_at=review_at,
        ),
    )

    jobs = [job for job in tool_ctx.pool.scheduled_jobs.values() if job["job_type"] == "oob_review"]
    assert len(jobs) == 1
    assert jobs[0]["user_id"] == tool_ctx.user.id
    assert jobs[0]["scheduled_for"] == review_at
    assert jobs[0]["context"]["oob_id"] == str(result.id)


async def test_check_oob_read_tool_passes_protected_owner_ids(tool_ctx, monkeypatch):
    calls = []

    async def fake_check_oob_with_policy(pool, *, content, recipient_id, protected_owner_ids=None, sender_intent=None):
        calls.append((pool, content, recipient_id, protected_owner_ids, sender_intent))
        return {
            "verdict": "ok",
            "reason": "checked",
            "triggering_oob_ids": [],
            "suggested_rewrite": None,
            "checker_failed": False,
        }

    monkeypatch.setattr(read_tools, "check_oob_with_policy", fake_check_oob_with_policy)
    protected_owner_ids = [tool_ctx.user.id, tool_ctx.partner.id]

    result = await read_tools.check_oob(
        tool_ctx,
        CheckOOBInput(
            content="draft",
            recipient_id=tool_ctx.partner.id,
            protected_owner_ids=protected_owner_ids,
            sender_intent="relay",
        ),
    )

    assert result["verdict"] == "ok"
    assert calls == [(tool_ctx.pool, "draft", tool_ctx.partner.id, protected_owner_ids, "relay")]


async def test_escalate_to_partner_passes_dyad_protected_owner_ids(tool_ctx, monkeypatch):
    sent = []
    tool_ctx.trigger_charge = "crisis"

    async def fake_send(pool, recipient, content, template_fallback=None, bot_turn_id=None, protected_owner_ids=None):
        sent.append((recipient, content, template_fallback, bot_turn_id, protected_owner_ids))
        return uuid4()

    monkeypatch.setattr(write_tools, "send_outbound", fake_send)

    await write_tools.escalate_to_partner(
        tool_ctx,
        EscalateToPartnerInput(
            from_user_id=tool_ctx.user.id,
            to_user_id=tool_ctx.partner.id,
            content="please check in",
            reason="crisis charge",
            is_crisis=True,
        ),
    )

    assert sent
    recipient, content, template_fallback, bot_turn_id, protected_owner_ids = sent[0]
    assert recipient.id == tool_ctx.partner.id
    assert content == "please check in"
    assert template_fallback.name == "escalation"
    assert bot_turn_id == tool_ctx.turn_id
    assert protected_owner_ids == [tool_ctx.user.id, tool_ctx.partner.id]


async def test_get_oob_returns_safe_summary_without_sensitive_core(tool_ctx):
    raw_core = "raw family secret must stay hidden"
    oob_id = uuid4()
    tool_ctx.pool.out_of_bounds[oob_id] = {
        "id": oob_id,
        "owner_id": tool_ctx.user.id,
        "sensitive_core": raw_core,
        "shareable_context": "family topic boundary",
        "severity": "firm",
        "status": "active",
        "created_at": datetime.now(UTC),
        "review_at": None,
    }

    result = await read_tools.get_oob(tool_ctx, GetOOBInput(owner_id=tool_ctx.user.id))

    dumped = result.model_dump(mode="json")
    assert dumped["entries"][0]["protected_summary"] == "family topic boundary"
    assert "sensitive_core" not in dumped["entries"][0]
    assert raw_core not in str(dumped)


async def test_output_shape_regressions(tool_ctx):
    style = await write_tools.update_user_style_notes(
        tool_ctx, UpdateUserStyleNotesInput(user_id=tool_ctx.user.id, notes="notes")
    )
    addressed = await write_tools.address_watch_item(
        tool_ctx,
        AddressWatchItemInput(watch_item_id=_seed_watch(tool_ctx.pool, tool_ctx.user.id), addressing_note="handled"),
    )
    lifted = await write_tools.lift_oob(tool_ctx, LiftOOBInput(oob_id=_seed_oob(tool_ctx.pool, tool_ctx.user.id)))

    assert style.user_id == tool_ctx.user.id and style.updated_at
    assert addressed.id and addressed.addressed_at
    assert lifted.id and lifted.lifted_at


async def test_log_observation_scores_when_significance_omitted(tool_ctx, monkeypatch):
    async def fake_score(pool, *, content, client=None):
        return 4, "material pattern", "v1"

    monkeypatch.setattr(write_tools.scoring, "score_observation", fake_score)
    result = await write_tools.log_observation(
        tool_ctx,
        LogObservationInput(content="obs", about_user_id=tool_ctx.user.id, confidence=Confidence.medium),
    )

    row = tool_ctx.pool.observations[result.id]
    assert row["significance"] == 4
    assert row["scoring_prompt_version"] == "v1"


async def test_log_observation_persists_failed_score_as_null(tool_ctx, monkeypatch):
    async def fake_score(pool, *, content, client=None):
        return None, "scoring failed: parse", "v1-failed"

    monkeypatch.setattr(write_tools.scoring, "score_observation", fake_score)
    result = await write_tools.log_observation(
        tool_ctx,
        LogObservationInput(content="obs", about_user_id=tool_ctx.user.id, confidence=Confidence.medium),
    )

    row = tool_ctx.pool.observations[result.id]
    assert row["significance"] is None
    assert row["scoring_prompt_version"] == "v1-failed"


async def test_log_observation_preserves_explicit_significance(tool_ctx, monkeypatch):
    called = False

    async def fake_score(pool, *, content, client=None):
        nonlocal called
        called = True
        return 1, "unused", "v1"

    monkeypatch.setattr(write_tools.scoring, "score_observation", fake_score)
    result = await write_tools.log_observation(
        tool_ctx,
        LogObservationInput(content="obs", about_user_id=tool_ctx.user.id, confidence=Confidence.medium, significance=5),
    )

    row = tool_ctx.pool.observations[result.id]
    assert row["significance"] == 5
    assert row["scoring_prompt_version"] == "v1"
    assert called is False


async def test_call_tool_validation_error_is_typed(tool_ctx):
    result = await call_tool(
        "log_observation",
        {"content": "obs", "about_user_id": None, "confidence": "medium", "significance": 7},
        tool_ctx,
    )

    assert result["is_error"] is True
    assert result["error"].startswith("validation:")


async def test_escalation_gate_rejects_before_outbound_and_allows_crisis(tool_ctx, monkeypatch):
    sent = []

    async def fake_send(pool, recipient, content, template_fallback=None, bot_turn_id=None, protected_owner_ids=None):
        sent.append((recipient, content, template_fallback, bot_turn_id, protected_owner_ids))
        return uuid4()

    monkeypatch.setattr(write_tools, "send_outbound", fake_send)
    before_messages = dict(tool_ctx.pool.messages)
    tool_ctx.trigger_charge = "charged"
    tool_ctx.explicit_partner_alert_requested = False
    rejected = await call_tool(
        "escalate_to_partner",
        {
            "from_user_id": str(uuid4()),
            "to_user_id": str(uuid4()),
            "content": "body",
            "reason": "weak",
            "is_crisis": False,
        },
        tool_ctx,
    )

    assert rejected["is_error"] is True
    assert not sent
    assert tool_ctx.pool.messages == before_messages

    still_rejected = await call_tool(
        "escalate_to_partner",
        {
            "from_user_id": str(tool_ctx.user.id),
            "to_user_id": str(tool_ctx.partner.id),
            "content": "body",
            "reason": "model claimed crisis but trusted context is not crisis",
            "is_crisis": True,
        },
        tool_ctx,
    )
    assert still_rejected["is_error"] is True
    assert not sent

    tool_ctx.trigger_charge = "crisis"
    allowed = await call_tool(
        "escalate_to_partner",
        {
            "from_user_id": str(uuid4()),
            "to_user_id": str(uuid4()),
            "content": "body",
            "reason": "crisis charge",
            "is_crisis": True,
        },
        tool_ctx,
    )

    assert allowed["action"] == "sent"
    assert sent[0][0].id == tool_ctx.partner.id
    assert sent[0][2].name == "escalation"
    assert sent[0][2].params == [tool_ctx.partner.name, tool_ctx.user.name, "body"]
    assert "ESCALATION_SENT gate=crisis" in tool_ctx.pool.bot_turns[tool_ctx.turn_id]["reasoning"]


async def test_escalation_allows_trusted_explicit_partner_alert(tool_ctx, monkeypatch):
    sent = []

    async def fake_send(pool, recipient, content, template_fallback=None, bot_turn_id=None, protected_owner_ids=None):
        sent.append((recipient, content, template_fallback, bot_turn_id, protected_owner_ids))
        return uuid4()

    monkeypatch.setattr(write_tools, "send_outbound", fake_send)
    tool_ctx.explicit_partner_alert_requested = True

    result = await write_tools.escalate_to_partner(
        tool_ctx,
        EscalateToPartnerInput(
            from_user_id=uuid4(),
            to_user_id=uuid4(),
            content="Please check in soon.",
            reason="trusted explicit partner alert request",
            is_crisis=False,
        ),
    )

    assert result.action == "sent"
    assert sent[0][0].id == tool_ctx.partner.id
    assert sent[0][2].name == "escalation"
    assert "ESCALATION_SENT gate=explicit_partner_alert" in tool_ctx.pool.bot_turns[tool_ctx.turn_id]["reasoning"]


async def test_phase_enforcement_returns_typed_errors(tool_ctx):
    tool_ctx.phase = "read"
    write_result = await call_tool("add_memory", {"about_user_id": str(tool_ctx.user.id), "content": "x"}, tool_ctx)
    tool_ctx.phase = "write"
    read_result = await call_tool("get_memories", {"about_user_id": str(tool_ctx.user.id)}, tool_ctx)

    assert write_result["is_error"] is True and write_result["error"].startswith("phase:")
    assert read_result["is_error"] is True and read_result["error"].startswith("phase:")


async def test_recent_activity_returns_period_and_stub_digest(tool_ctx):
    tool_ctx.phase = "read"
    sent_at = datetime.now(UTC) - timedelta(hours=2)
    message_id = uuid4()
    tool_ctx.pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": tool_ctx.user.id,
        "recipient_id": None,
        "content": "latest context",
        "processing_state": "processed",
        "sent_at": sent_at,
        "charge": "routine",
        "deleted_at": None,
    }

    result = await call_tool("recent_activity", RecentActivityInput(days=7).model_dump(mode="json"), tool_ctx)

    assert "error" not in result
    assert result["period"]["start"] is not None
    assert result["period"]["end"] is not None
    user_thread = next(thread for thread in result["threads"] if thread["user_id"] == str(tool_ctx.user.id))
    assert user_thread["message_count"] == 1
    assert user_thread["summary"] == '1 messages this period; latest: "latest context"'


async def test_get_bot_actions_includes_trigger_and_outbound_content(tool_ctx):
    tool_ctx.phase = "read"
    inbound_id = uuid4()
    outbound_id = uuid4()
    tool_ctx.pool.messages[inbound_id] = {
        "id": inbound_id,
        "direction": "inbound",
        "sender_id": tool_ctx.user.id,
        "recipient_id": None,
        "content": "why did you tell her that?",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "deleted_at": None,
    }
    tool_ctx.pool.messages[outbound_id] = {
        "id": outbound_id,
        "direction": "outbound",
        "sender_id": None,
        "recipient_id": tool_ctx.user.id,
        "content": "because you asked me to",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": None,
        "deleted_at": None,
    }
    tool_ctx.pool.bot_turns[tool_ctx.turn_id].update(
        started_at=datetime.now(UTC),
        user_in_context=tool_ctx.user.id,
        triggered_by_message_id=inbound_id,
        final_output_message_id=outbound_id,
        reasoning="explicit request",
        triggering_message_ids=[inbound_id],
    )
    tool_ctx.pool.tool_calls.append({"turn_id": tool_ctx.turn_id, "tool_name": "escalate_to_partner", "arguments": {}, "result": {}, "called_at": datetime.now(UTC), "duration_ms": 1})

    result = await call_tool("get_bot_actions", {"target_type": "escalation"}, tool_ctx)

    action = result["actions"][0]
    assert action["triggering_content"] == "why did you tell her that?"
    assert action["final_outbound_content"] == "because you asked me to"
    assert action["tool_calls"][0]["tool_name"] == "escalate_to_partner"
