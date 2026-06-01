from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone
from dataclasses import replace
from uuid import uuid4

import pytest

from app.config import get_settings
from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services.cross_thread_privacy import (
    bridge_candidate_visible_to_target,
    can_view_raw_message,
    normalize_partner_share_for_privacy,
    raw_message_visibility,
    redact_raw_message_content,
    should_omit_raw_message,
)
from app.services.turn_context import TurnContext
from app.services.scheduled_job_handlers import schedule_checkin_job
from app.services.tools import read_tools, write_tools
from app.services.tools.common import current_scheduled_task
from app.services.tools.registry import call_tool
from tool_schemas import (
    AddDistillationInput,
    AddMemoryInput,
    AddOOBInput,
    AddWatchItemInput,
    AddressWatchItemInput,
    CancelScheduledCheckinInput,
    CancelScheduledTaskInput,
    CheckOOBInput,
    Confidence,
    CreateBridgeCandidateInput,
    CreateThemeInput,
    CrossThreadSharingDefault,
    EditOutboundMessageInput,
    EscalateToPartnerInput,
    ExplainMediaItemInput,
    FeedbackSentiment,
    GetDistillationsInput,
    GetMemoriesInput,
    GetOOBInput,
    ListBridgeCandidatesInput,
    ListScheduledTasksInput,
    LiftOOBInput,
    LogFeedbackInput,
    LogObservationInput,
    OOBSeverity,
    RecentActivityInput,
    ScheduleCheckinInput,
    ScheduleDelay,
    ScheduleTaskInput,
    LocalScheduleTime,
    SearchMessagesInput,
    SearchEmojisInput,
    SendBridgeCandidateInput,
    ReviseDistillationInput,
    SetPartnerSharingInput,
    SupersedeMemoryInput,
    ThemeHealth,
    ThemeSentiment,
    UpdateBridgeCandidateInput,
    UpdateDistillationInput,
    UpdateMemoryInput,
    UpdateOOBInput,
    UpdateObservationInput,
    UpdateScheduledTaskInput,
    UpdateThemeInput,
    UpdateUserStyleNotesInput,
    UpdateWatchItemInput,
)

pytestmark = pytest.mark.anyio


def test_cross_thread_privacy_helper_gates_raw_partner_messages():
    viewer_id = uuid4()
    partner_id = uuid4()

    assert normalize_partner_share_for_privacy(None) == "unset"
    assert (
        normalize_partner_share_for_privacy(CrossThreadSharingDefault.opt_in)
        == "opt_in"
    )
    assert can_view_raw_message(
        viewer_user_id=viewer_id,
        thread_owner_user_id=viewer_id,
        thread_owner_partner_share=None,
    )
    assert not can_view_raw_message(
        viewer_user_id=viewer_id,
        thread_owner_user_id=partner_id,
        thread_owner_partner_share=None,
    )
    assert not can_view_raw_message(
        viewer_user_id=viewer_id,
        thread_owner_user_id=partner_id,
        thread_owner_partner_share="opt_out",
    )
    assert can_view_raw_message(
        viewer_user_id=viewer_id,
        thread_owner_user_id=partner_id,
        thread_owner_partner_share="opt_in",
    )

    visibility = raw_message_visibility(
        viewer_user_id=viewer_id,
        thread_owner_user_id=partner_id,
        thread_owner_partner_share=None,
    )
    assert visibility.reason == "thread_owner_partner_share_not_opted_in"
    assert visibility.omission_reason == "raw_partner_content_hidden_by_partner_share"
    assert should_omit_raw_message(
        viewer_user_id=viewer_id,
        thread_owner_user_id=partner_id,
        thread_owner_partner_share=None,
    )
    assert "withheld" in redact_raw_message_content(
        "private raw text",
        viewer_user_id=viewer_id,
        thread_owner_user_id=partner_id,
        thread_owner_partner_share=None,
    )


def test_cross_thread_privacy_helper_gates_bridge_target_visibility():
    target_id = uuid4()
    source_id = uuid4()

    for status in ("ready", "sent", "addressed"):
        assert bridge_candidate_visible_to_target(
            {"status": status, "target_user_id": target_id},
            target_user_id=target_id,
        )
    for status in ("pending", "declined", "blocked", "expired"):
        assert not bridge_candidate_visible_to_target(
            {"status": status, "target_user_id": target_id},
            target_user_id=target_id,
        )
    assert not bridge_candidate_visible_to_target(
        {"status": "ready", "target_user_id": source_id},
        target_user_id=target_id,
    )


@pytest.fixture
def tool_ctx(fake_pool):
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
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "reasoning": "",
        "completed_at": None,
        "failure_reason": None,
    }
    return TurnContext(
        turn_id,
        fake_pool,
        user,
        partner,
        [uuid4()],
        current_step="record",
        bot_id="mediator",
        user_id=user.id,
        primary_topic_id=get_relationship_topic_id(),
        dyad_id=uuid4(),
    )


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


def _set_partner_share(pool, user_id, bot_id, partner_share="opt_in"):
    pool.user_bot_state[(user_id, bot_id)] = {
        "user_id": user_id,
        "bot_id": bot_id,
        "partner_share": partner_share,
    }


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


def test_current_scheduled_task_helper_is_scoped_to_scheduled_task_turns(tool_ctx):
    assert current_scheduled_task(tool_ctx) is None

    job_id = uuid4()
    task_id = uuid4()
    recurrence = {"type": "daily", "interval": 1}
    scheduled_ctx = TurnContext(
        tool_ctx.turn_id,
        tool_ctx.pool,
        tool_ctx.user,
        tool_ctx.partner,
        [],
        current_step="schedule",
        trigger_metadata={
            "kind": "scheduled_task",
            "context": {
                "job_id": str(job_id),
                "task_id": str(task_id),
                "brief": "Send a short future brief",
                "recurrence": recurrence,
            },
        },
    )

    assert current_scheduled_task(scheduled_ctx) == {
        "job_id": str(job_id),
        "task_id": str(task_id),
        "brief": "Send a short future brief",
        "recurrence": recurrence,
    }
    inbound_ctx = TurnContext(
        tool_ctx.turn_id,
        tool_ctx.pool,
        tool_ctx.user,
        tool_ctx.partner,
        [uuid4()],
        current_step="record",
        trigger_metadata={
            "kind": "inbound",
            "context": {
                "job_id": str(job_id),
                "task_id": str(task_id),
                "brief": "ignore",
            },
        },
    )
    assert current_scheduled_task(inbound_ctx) is None


async def test_scheduled_task_current_task_requires_scheduled_task_turn(tool_ctx):
    tool_ctx.current_step = "schedule"
    update_result = await call_tool(
        "update_scheduled_task",
        {"current_task": True, "brief": "Update the current task."},
        tool_ctx,
    )
    cancel_result = await call_tool(
        "cancel_scheduled_task",
        {"current_task": True, "reason": "No longer needed."},
        tool_ctx,
    )

    assert update_result["is_error"] is True
    assert (
        "current_task=true is only valid during a scheduled_task turn"
        in update_result["error"]
    )
    assert cancel_result["is_error"] is True
    assert (
        "current_task=true is only valid during a scheduled_task turn"
        in cancel_result["error"]
    )


async def test_scheduled_task_tools_create_list_update_cancel_and_audit(tool_ctx):
    scheduled_for = datetime.now(UTC) + timedelta(days=2)
    recurrence_until = scheduled_for + timedelta(days=30)
    created = await write_tools.schedule_task(
        tool_ctx,
        ScheduleTaskInput(
            brief="Prepare a morning repair brief.",
            when=scheduled_for,
            recurrence={"type": "daily", "interval": 1, "until": recurrence_until},
        ),
    )

    job = tool_ctx.pool.scheduled_jobs[created.job_id]
    assert job["user_id"] == tool_ctx.user.id
    assert job["job_type"] == "scheduled_task"
    assert job["status"] == "pending"
    assert job["context"]["task_id"] == str(created.task_id)
    assert job["context"]["brief"] == "Prepare a morning repair brief."
    assert created.scheduled_for == scheduled_for

    partner_job_id = uuid4()
    tool_ctx.pool.scheduled_jobs[partner_job_id] = {
        **job,
        "id": partner_job_id,
        "user_id": tool_ctx.partner.id,
        "context": {
            **job["context"],
            "task_id": str(uuid4()),
            "brief": "Partner-only task.",
        },
    }

    listed = await write_tools.list_scheduled_tasks(tool_ctx, ListScheduledTasksInput())
    assert [task.job_id for task in listed.tasks] == [created.job_id]
    assert listed.tasks[0].task_id == created.task_id
    assert listed.tasks[0].recurrence_until_time is not None
    assert listed.tasks[0].recurrence_until_time.utc == recurrence_until.isoformat()

    updated_for = scheduled_for + timedelta(days=1, hours=1, minutes=15)
    updated = await write_tools.update_scheduled_task(
        tool_ctx,
        UpdateScheduledTaskInput(
            task_id=created.task_id,
            brief="Prepare an updated repair brief.",
            when=updated_for,
            recurrence=None,
        ),
    )
    assert updated.action == "updated"
    assert updated.job_id == created.job_id
    assert updated.task_id == created.task_id
    assert updated.recurrence is None
    assert tool_ctx.pool.scheduled_jobs[created.job_id]["scheduled_for"] == updated_for
    assert (
        tool_ctx.pool.scheduled_jobs[created.job_id]["context"]["brief"]
        == "Prepare an updated repair brief."
    )
    assert tool_ctx.pool.scheduled_jobs[created.job_id]["context"]["recurrence"] is None

    cancelled = await write_tools.cancel_scheduled_task(
        tool_ctx,
        CancelScheduledTaskInput(job_id=created.job_id, reason="No longer useful."),
    )
    assert cancelled.action == "cancelled"
    assert cancelled.job_id == created.job_id
    assert cancelled.task_id == created.task_id
    assert tool_ctx.pool.scheduled_jobs[created.job_id]["status"] == "cancelled"
    assert tool_ctx.pool.scheduled_jobs[partner_job_id]["status"] == "pending"
    assert [row["tool_name"] for row in tool_ctx.pool.tool_calls[-4:]] == [
        "schedule_task",
        "list_scheduled_tasks",
        "update_scheduled_task",
        "cancel_scheduled_task",
    ]


async def test_scheduled_task_tools_reject_past_times(tool_ctx):
    with pytest.raises(write_tools.ToolCallRejected) as create_exc:
        await write_tools.schedule_task(
            tool_ctx,
            ScheduleTaskInput(
                brief="Past task.", when=datetime.now(UTC) - timedelta(minutes=1)
            ),
        )
    assert create_exc.value.result["error"] == "schedule_time_in_past"

    created = await write_tools.schedule_task(
        tool_ctx,
        ScheduleTaskInput(
            brief="Future task.", when=datetime.now(UTC) + timedelta(days=1)
        ),
    )
    with pytest.raises(write_tools.ToolCallRejected) as update_exc:
        await write_tools.update_scheduled_task(
            tool_ctx,
            UpdateScheduledTaskInput(
                task_id=created.task_id, when=datetime.now(UTC) - timedelta(minutes=1)
            ),
        )
    assert update_exc.value.result["error"] == "schedule_time_in_past"


async def test_scheduled_task_tools_accept_relative_delay(tool_ctx):
    before = datetime.now(UTC)
    created = await write_tools.schedule_task(
        tool_ctx,
        ScheduleTaskInput(brief="Relative task.", delay=ScheduleDelay(days=2)),
    )
    scheduled_for = tool_ctx.pool.scheduled_jobs[created.job_id]["scheduled_for"]

    assert (
        before + timedelta(days=2)
        <= scheduled_for
        <= datetime.now(UTC) + timedelta(days=2, seconds=1)
    )


async def test_scheduled_task_and_update_accept_local_berlin_clock_time(tool_ctx):
    berlin_user = User(
        tool_ctx.user.id, tool_ctx.user.name, tool_ctx.user.phone, "Europe/Berlin"
    )
    berlin_ctx = TurnContext(
        tool_ctx.turn_id,
        tool_ctx.pool,
        berlin_user,
        tool_ctx.partner,
        tool_ctx.triggering_message_ids,
        current_step=tool_ctx.current_step,
    )

    created = await write_tools.schedule_task(
        berlin_ctx,
        ScheduleTaskInput(
            brief="Internal agent task for 9pm Berlin.",
            local_when=LocalScheduleTime(date=date(2036, 5, 6), time=time(21, 0)),
        ),
    )

    assert tool_ctx.pool.scheduled_jobs[created.job_id]["scheduled_for"] == datetime(
        2036, 5, 6, 19, 0, tzinfo=UTC
    )

    updated = await write_tools.update_scheduled_task(
        berlin_ctx,
        UpdateScheduledTaskInput(
            task_id=created.task_id,
            local_when=LocalScheduleTime(
                date=date(2036, 5, 7), time=time(9, 30), timezone="Europe/Berlin"
            ),
        ),
    )

    assert updated.scheduled_for == datetime(2036, 5, 7, 7, 30, tzinfo=UTC)


async def test_scheduled_task_current_task_update_and_cancel_mutate_current_row(
    tool_ctx,
):
    scheduled_for = datetime.now(UTC) + timedelta(days=2)
    created = await write_tools.schedule_task(
        tool_ctx,
        ScheduleTaskInput(brief="Current brief.", when=scheduled_for),
    )
    current_ctx = TurnContext(
        tool_ctx.turn_id,
        tool_ctx.pool,
        tool_ctx.user,
        tool_ctx.partner,
        [],
        current_step="schedule",
        trigger_metadata={
            "kind": "scheduled_task",
            "context": {
                "job_id": str(created.job_id),
                "task_id": str(created.task_id),
                "brief": "Current brief.",
                "recurrence": None,
            },
        },
    )

    updated = await write_tools.update_scheduled_task(
        current_ctx,
        UpdateScheduledTaskInput(
            current_task=True,
            brief="Updated from inside the scheduled-task turn.",
            recurrence={"type": "weekly", "weekdays": [1], "interval": 1},
        ),
    )
    cancelled = await write_tools.cancel_scheduled_task(
        current_ctx,
        CancelScheduledTaskInput(current_task=True, reason="Cancel after this run."),
    )

    row = tool_ctx.pool.scheduled_jobs[created.job_id]
    assert updated.action == "updated"
    assert updated.job_id == created.job_id
    assert cancelled.action == "cancelled"
    assert row["status"] == "pending"
    assert row["context"]["brief"] == "Updated from inside the scheduled-task turn."
    assert row["context"]["recurrence"] == {
        "version": 1,
        "type": "weekly",
        "interval": 1,
        "weekdays": [1],
    }
    assert row["context"]["scheduled_task_control"] == {
        "cancel_after_current_fire": True,
        "reason": "Cancel after this run.",
    }


async def test_scheduled_task_current_task_call_tool_accepts_scheduled_turn(tool_ctx):
    scheduled_for = datetime.now(UTC) + timedelta(days=2)
    created = await write_tools.schedule_task(
        tool_ctx,
        ScheduleTaskInput(brief="Current call_tool brief.", when=scheduled_for),
    )
    current_ctx = TurnContext(
        tool_ctx.turn_id,
        tool_ctx.pool,
        tool_ctx.user,
        tool_ctx.partner,
        [],
        current_step="schedule",
        trigger_metadata={
            "kind": "scheduled_task",
            "context": {
                "job_id": str(created.job_id),
                "task_id": str(created.task_id),
                "brief": "Current call_tool brief.",
                "recurrence": None,
            },
        },
    )

    update_result = await call_tool(
        "update_scheduled_task",
        {"current_task": True, "brief": "Updated through call_tool."},
        current_ctx,
    )
    cancel_result = await call_tool(
        "cancel_scheduled_task",
        {"current_task": True, "reason": "Cancel through call_tool."},
        current_ctx,
    )

    row = tool_ctx.pool.scheduled_jobs[created.job_id]
    assert update_result["action"] == "updated"
    assert update_result["job_id"] == str(created.job_id)
    assert cancel_result["action"] == "cancelled"
    assert row["status"] == "pending"
    assert row["context"]["brief"] == "Updated through call_tool."
    assert row["context"]["scheduled_task_control"] == {
        "cancel_after_current_fire": True,
        "reason": "Cancel through call_tool.",
    }


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


def _seed_distillation(
    pool,
    source_user_id,
    *,
    related_observation_ids=None,
    supporting_message_ids=None,
):
    distillation_id = uuid4()
    pool.distillations[distillation_id] = {
        "id": distillation_id,
        "content": "One possible explanation is that repair feels risky after prior withdrawal.",
        "confidence": "medium",
        "status": "active",
        "sensitivity": "medium",
        "visibility": "private",
        "shareable_summary": None,
        "source_user_ids": [source_user_id],
        "related_memory_ids": [],
        "related_observation_ids": list(related_observation_ids or []),
        "related_theme_ids": [],
        "supporting_message_ids": list(supporting_message_ids or []),
        "created_from_tool_call_id": None,
        "triggering_message_id": None,
        "supersedes_distillation_id": None,
        "superseded_by_distillation_id": None,
        "revision_note": None,
        "revision_count": 0,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "revised_at": None,
        "retired_at": None,
    }
    return distillation_id


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


def _seed_message(
    pool,
    user_id,
    partner_id,
    *,
    direction="inbound",
    content="source",
    bot_id="mediator",
    topic_id=None,
):
    message_id = uuid4()
    pool.messages[message_id] = {
        "id": message_id,
        "direction": direction,
        "sender_id": user_id if direction == "inbound" else None,
        "recipient_id": partner_id if direction == "inbound" else user_id,
        "content": content,
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "whatsapp_message_id": None,
        "deleted_at": None,
        "bot_id": bot_id,
        "topic_id": topic_id or get_relationship_topic_id(),
    }
    return message_id


def _distillation_add_call(ctx):
    observation_id = _seed_observation(ctx.pool, ctx.user.id)
    message_id = _seed_message(ctx.pool, ctx.user.id, ctx.partner.id)
    return (
        write_tools.add_distillation,
        AddDistillationInput(
            content="One possible explanation is that repair feels risky after prior withdrawal.",
            source_user_ids=[ctx.user.id],
            related_observation_ids=[observation_id],
            supporting_message_ids=[message_id],
        ),
    )


def _distillation_update_call(ctx):
    observation_id = _seed_observation(ctx.pool, ctx.user.id)
    distillation_id = _seed_distillation(
        ctx.pool, ctx.user.id, related_observation_ids=[observation_id]
    )
    return (
        write_tools.update_distillation,
        UpdateDistillationInput(
            distillation_id=distillation_id, revision_note="wording cleanup"
        ),
    )


def _distillation_revise_call(ctx):
    observation_id = _seed_observation(ctx.pool, ctx.user.id)
    message_id = _seed_message(ctx.pool, ctx.user.id, ctx.partner.id)
    distillation_id = _seed_distillation(
        ctx.pool, ctx.user.id, related_observation_ids=[observation_id]
    )
    return (
        write_tools.revise_distillation,
        ReviseDistillationInput(
            old_distillation_id=distillation_id,
            new_content="One possible revised explanation is that repair feels pressured when timing is rushed.",
            source_user_ids=[ctx.user.id],
            related_observation_ids=[observation_id],
            supporting_message_ids=[message_id],
            revision_note="new observation changed the synthesis",
        ),
    )


@pytest.mark.parametrize(
    ("tool_name", "call_factory"),
    [
        (
            "update_user_style_notes",
            lambda ctx: (
                write_tools.update_user_style_notes,
                UpdateUserStyleNotesInput(user_id=ctx.user.id, notes="short"),
            ),
        ),
        (
            "set_partner_sharing",
            lambda ctx: (
                write_tools.set_partner_sharing,
                SetPartnerSharingInput(opt_in=True, reason="user chose opt in"),
            ),
        ),
        (
            "add_memory",
            lambda ctx: (
                write_tools.add_memory,
                AddMemoryInput(about_user_id=ctx.user.id, content="memory"),
            ),
        ),
        (
            "update_memory",
            lambda ctx: (
                write_tools.update_memory,
                UpdateMemoryInput(
                    memory_id=_seed_memory(ctx.pool, ctx.user.id), content="new"
                ),
            ),
        ),
        (
            "supersede_memory",
            lambda ctx: (
                write_tools.supersede_memory,
                SupersedeMemoryInput(
                    old_memory_id=_seed_memory(ctx.pool, ctx.user.id), new_content="new"
                ),
            ),
        ),
        (
            "create_theme",
            lambda ctx: (
                write_tools.create_theme,
                CreateThemeInput(
                    title="Theme",
                    description="desc",
                    sentiment=ThemeSentiment.mixed,
                    health=ThemeHealth.tender,
                ),
            ),
        ),
        (
            "update_theme",
            lambda ctx: (
                write_tools.update_theme,
                UpdateThemeInput(theme_id=_seed_theme(ctx.pool), mark_reinforced=True),
            ),
        ),
        (
            "add_watch_item",
            lambda ctx: (
                write_tools.add_watch_item,
                AddWatchItemInput(owner_user_id=ctx.user.id, content="watch"),
            ),
        ),
        (
            "update_watch_item",
            lambda ctx: (
                write_tools.update_watch_item,
                UpdateWatchItemInput(
                    watch_item_id=_seed_watch(ctx.pool, ctx.user.id), content="new"
                ),
            ),
        ),
        (
            "address_watch_item",
            lambda ctx: (
                write_tools.address_watch_item,
                AddressWatchItemInput(
                    watch_item_id=_seed_watch(ctx.pool, ctx.user.id),
                    addressing_note="handled",
                ),
            ),
        ),
        (
            "log_observation",
            lambda ctx: (
                write_tools.log_observation,
                LogObservationInput(
                    content="obs",
                    about_user_id=ctx.user.id,
                    confidence=Confidence.medium,
                    significance=3,
                ),
            ),
        ),
        (
            "update_observation",
            lambda ctx: (
                write_tools.update_observation,
                UpdateObservationInput(
                    observation_id=_seed_observation(ctx.pool, ctx.user.id),
                    content="new",
                ),
            ),
        ),
        ("add_distillation", _distillation_add_call),
        ("update_distillation", _distillation_update_call),
        ("revise_distillation", _distillation_revise_call),
        (
            "add_oob",
            lambda ctx: (
                write_tools.add_oob,
                AddOOBInput(
                    owner_id=ctx.user.id,
                    sensitive_core="private",
                    severity=OOBSeverity.firm,
                ),
            ),
        ),
        (
            "update_oob",
            lambda ctx: (
                write_tools.update_oob,
                UpdateOOBInput(
                    oob_id=_seed_oob(ctx.pool, ctx.user.id), sensitive_core="new"
                ),
            ),
        ),
        (
            "lift_oob",
            lambda ctx: (
                write_tools.lift_oob,
                LiftOOBInput(oob_id=_seed_oob(ctx.pool, ctx.user.id)),
            ),
        ),
        (
            "schedule_checkin",
            lambda ctx: (
                write_tools.schedule_checkin,
                ScheduleCheckinInput(
                    user_id=ctx.user.id,
                    when=datetime.now(UTC) + timedelta(hours=2),
                    about_what="talk",
                    reason="follow up",
                ),
            ),
        ),
        (
            "cancel_scheduled_checkin",
            lambda ctx: (
                _seed_job(ctx.pool, ctx.user.id)
                and write_tools.cancel_scheduled_checkin,
                CancelScheduledCheckinInput(user_id=ctx.user.id),
            ),
        ),
        (
            "escalate_to_partner",
            lambda ctx: (
                write_tools.escalate_to_partner,
                EscalateToPartnerInput(
                    from_user_id=ctx.user.id,
                    to_user_id=ctx.partner.id,
                    content="body",
                    reason="crisis charge",
                    is_crisis=True,
                ),
            ),
        ),
        (
            "explain_media_item",
            lambda ctx: (
                write_tools.explain_media_item,
                ExplainMediaItemInput(
                    message_id=_seed_message(ctx.pool, ctx.user.id, ctx.partner.id),
                    reason="fresh explanation",
                ),
            ),
        ),
        (
            "log_feedback",
            lambda ctx: (
                write_tools.log_feedback,
                LogFeedbackInput(
                    from_user_id=ctx.user.id,
                    target_type="general",
                    target_id=None,
                    sentiment=FeedbackSentiment.positive,
                    content="good",
                ),
            ),
        ),
    ],
)
async def test_every_write_tool_inserts_tool_call(
    tool_ctx, monkeypatch, tool_name, call_factory
):
    sent = []

    async def fake_send(
        pool,
        recipient,
        content,
        *,
        template_fallback=None,
        bot_turn_id=None,
        protected_owner_ids=None,
        scope,
    ):
        assert scope.bot_id == tool_ctx.bot_id
        assert scope.topic_id == tool_ctx.primary_topic_id
        sent.append(
            (recipient, content, template_fallback, bot_turn_id, protected_owner_ids)
        )
        return uuid4()

    async def fake_explain(pool, message_id):
        return {"explanation": "image note"}

    monkeypatch.setattr(write_tools, "send_outbound", fake_send)
    monkeypatch.setattr(write_tools, "explain_stored_image", fake_explain)
    fn, args = call_factory(tool_ctx)
    if tool_name == "explain_media_item":
        tool_ctx.pool.messages[args.message_id]["media_type"] = "image"
        tool_ctx.pool.messages[args.message_id][
            "media_url"
        ] = f"mediator-media/image/{args.message_id}"
    if tool_name == "escalate_to_partner":
        tool_ctx.trigger_charge = "crisis"

    await fn(tool_ctx, args)

    row = tool_ctx.pool.tool_calls[-1]
    assert row["turn_id"] == tool_ctx.turn_id
    assert row["tool_name"] == tool_name
    assert isinstance(row["arguments"], dict)
    assert isinstance(row["result"], dict)
    assert row["duration_ms"] is not None


async def test_distillation_read_write_update_revise_lifecycle_preserves_observations(
    tool_ctx,
):
    observation_id = _seed_observation(tool_ctx.pool, tool_ctx.user.id)
    source_message_id = _seed_message(
        tool_ctx.pool, tool_ctx.user.id, tool_ctx.partner.id
    )
    tool_ctx.triggering_message_ids = [source_message_id]

    created = await write_tools.add_distillation(
        tool_ctx,
        AddDistillationInput(
            content="One possible explanation is that repair feels risky after prior withdrawal.",
            source_user_ids=[tool_ctx.user.id],
            related_observation_ids=[observation_id],
        ),
    )

    created_row = tool_ctx.pool.distillations[created.id]
    assert created_row["supporting_message_ids"] == [source_message_id]
    assert created_row["triggering_message_id"] == source_message_id
    assert created_row["related_observation_ids"] == [observation_id]

    updated = await write_tools.update_distillation(
        tool_ctx,
        UpdateDistillationInput(
            distillation_id=created.id,
            shareable_summary="Repair may feel pressured when timing is rushed.",
            visibility="dyad_shareable",
            revision_note="safe summary added",
        ),
    )
    assert updated.id == created.id
    assert tool_ctx.pool.distillations[created.id]["visibility"] == "dyad_shareable"
    assert (
        tool_ctx.pool.distillations[created.id]["shareable_summary"]
        == "Repair may feel pressured when timing is rushed."
    )

    revised = await write_tools.revise_distillation(
        tool_ctx,
        ReviseDistillationInput(
            old_distillation_id=created.id,
            new_content="One possible revised explanation is that repair feels pressured when it arrives before Maya has calmed down.",
            source_user_ids=[tool_ctx.user.id],
            related_observation_ids=[observation_id],
            revision_note="new observation narrowed the synthesis",
        ),
    )

    old_row = tool_ctx.pool.distillations[created.id]
    new_row = tool_ctx.pool.distillations[revised.new_id]
    assert old_row["status"] == "revised"
    assert old_row["superseded_by_distillation_id"] == revised.new_id
    assert new_row["supersedes_distillation_id"] == created.id
    assert new_row["revision_count"] == 1
    assert observation_id in tool_ctx.pool.observations

    read_ctx = TurnContext(
        tool_ctx.turn_id,
        tool_ctx.pool,
        tool_ctx.user,
        tool_ctx.partner,
        [],
        current_step="read",
    )
    found = await read_tools.get_distillations(
        read_ctx,
        GetDistillationsInput(related_observation_id=observation_id, limit=10),
    )
    assert [row.id for row in found.distillations] == [revised.new_id]
    assert [row["tool_name"] for row in tool_ctx.pool.tool_calls[-3:]] == [
        "add_distillation",
        "update_distillation",
        "revise_distillation",
    ]


async def test_add_memory_persists_shareable_summary(tool_ctx):
    created = await write_tools.add_memory(
        tool_ctx,
        AddMemoryInput(
            about_user_id=tool_ctx.user.id,
            content="Maya wants Ben to know the appointment timing matters.",
            visibility="dyad_shareable",
            shareable_summary="Maya wants Ben to know the appointment timing matters.",
        ),
    )

    row = tool_ctx.pool.memories[created.id]
    assert row["visibility"] == "dyad_shareable"
    assert row["shareable_summary"] == (
        "Maya wants Ben to know the appointment timing matters."
    )
    assert row["shareable_summary_encrypted"] is not None
    assert row["recorded_by_bot_id"] == "mediator"


async def test_get_memories_gates_partner_rows_and_returns_summary_only(tool_ctx):
    private_id = _seed_memory(tool_ctx.pool, tool_ctx.partner.id)
    shareable_id = _seed_memory(tool_ctx.pool, tool_ctx.partner.id)
    tool_ctx.pool.memories[private_id]["content"] = "partner private memory"
    tool_ctx.pool.memories[private_id]["recorded_by_bot_id"] = "mediator"
    tool_ctx.pool.memories[shareable_id]["content"] = "partner full memory"
    tool_ctx.pool.memories[shareable_id]["visibility"] = "dyad_shareable"
    tool_ctx.pool.memories[shareable_id][
        "shareable_summary"
    ] = "partner safe memory summary"
    tool_ctx.pool.memories[shareable_id]["recorded_by_bot_id"] = "mediator"

    read_ctx = TurnContext(
        tool_ctx.turn_id,
        tool_ctx.pool,
        tool_ctx.user,
        tool_ctx.partner,
        [],
        current_step="read",
        bot_id="mediator",
        user_id=tool_ctx.user.id,
        primary_topic_id=get_relationship_topic_id(),
    )
    hidden = await read_tools.get_memories(read_ctx, GetMemoriesInput(scope="all"))
    assert private_id not in [row.id for row in hidden.memories]
    assert shareable_id not in [row.id for row in hidden.memories]

    _set_partner_share(tool_ctx.pool, tool_ctx.partner.id, "mediator", "opt_out")
    opted_out = await read_tools.get_memories(read_ctx, GetMemoriesInput(scope="all"))
    assert private_id not in [row.id for row in opted_out.memories]
    assert shareable_id not in [row.id for row in opted_out.memories]

    _set_partner_share(tool_ctx.pool, tool_ctx.partner.id, "mediator", "opt_in")
    visible = await read_tools.get_memories(read_ctx, GetMemoriesInput(scope="all"))

    assert private_id not in [row.id for row in visible.memories]
    assert [
        (row.id, row.content) for row in visible.memories if row.id == shareable_id
    ] == [(shareable_id, "partner safe memory summary")]


async def test_get_distillations_gates_hidden_partner_sources(tool_ctx):
    observation_id = _seed_observation(tool_ctx.pool, tool_ctx.partner.id)
    private_id = _seed_distillation(
        tool_ctx.pool, tool_ctx.partner.id, related_observation_ids=[observation_id]
    )
    shareable_id = _seed_distillation(
        tool_ctx.pool, tool_ctx.partner.id, related_observation_ids=[observation_id]
    )
    tool_ctx.pool.distillations[shareable_id]["visibility"] = "dyad_shareable"
    tool_ctx.pool.distillations[shareable_id][
        "shareable_summary"
    ] = "A reviewed safe summary."
    tool_ctx.pool.distillations[private_id]["recorded_by_bot_id"] = "mediator"
    tool_ctx.pool.distillations[shareable_id]["recorded_by_bot_id"] = "mediator"

    read_ctx = TurnContext(
        tool_ctx.turn_id,
        tool_ctx.pool,
        tool_ctx.user,
        tool_ctx.partner,
        [],
        current_step="read",
    )
    result = await read_tools.get_distillations(
        read_ctx, GetDistillationsInput(limit=10)
    )

    assert private_id not in [row.id for row in result.distillations]
    assert shareable_id not in [row.id for row in result.distillations]

    _set_partner_share(tool_ctx.pool, tool_ctx.partner.id, "mediator", "opt_out")
    result = await read_tools.get_distillations(
        read_ctx, GetDistillationsInput(limit=10)
    )
    assert private_id not in [row.id for row in result.distillations]
    assert shareable_id not in [row.id for row in result.distillations]

    _set_partner_share(tool_ctx.pool, tool_ctx.partner.id, "mediator", "opt_in")
    result = await read_tools.get_distillations(
        read_ctx, GetDistillationsInput(limit=10)
    )

    assert private_id not in [row.id for row in result.distillations]
    assert [(row.id, row.content) for row in result.distillations] == [
        (shareable_id, "A reviewed safe summary.")
    ]


async def test_supersede_memory_flips_old_and_links_new(tool_ctx):
    old_id = _seed_memory(tool_ctx.pool, tool_ctx.user.id)

    result = await write_tools.supersede_memory(
        tool_ctx,
        SupersedeMemoryInput(old_memory_id=old_id, new_content="replacement"),
    )

    assert tool_ctx.pool.memories[old_id]["status"] == "superseded"
    assert tool_ctx.pool.memories[result.new_id]["supersedes_memory_id"] == old_id


async def test_set_partner_sharing_records_scoped_choice(tool_ctx):
    result = await write_tools.set_partner_sharing(
        tool_ctx,
        SetPartnerSharingInput(opt_in=False, reason="wants privacy by default"),
    )

    assert result.partner_share == "opt_out"
    assert result.user_id == tool_ctx.user.id
    assert result.bot_id == "mediator"
    assert (
        tool_ctx.pool.user_bot_state[(tool_ctx.user.id, "mediator")]["partner_share"]
        == "opt_out"
    )
    assert tool_ctx.pool.tool_calls[-1]["tool_name"] == "set_partner_sharing"

    tool_ctx.bot_id = "tante_rosi"
    result = await write_tools.set_partner_sharing(
        tool_ctx,
        SetPartnerSharingInput(opt_in=True, reason="share pregnancy updates"),
    )
    assert result.partner_share == "opt_in"
    assert result.user_id == tool_ctx.user.id
    assert result.bot_id == "tante_rosi"
    assert (
        tool_ctx.pool.user_bot_state[(tool_ctx.user.id, "mediator")]["partner_share"]
        == "opt_out"
    )
    assert (
        tool_ctx.pool.user_bot_state[(tool_ctx.user.id, "tante_rosi")]["partner_share"]
        == "opt_in"
    )

    tool_ctx.bot_id = None
    with pytest.raises(write_tools.ToolCallRejected):
        await write_tools.set_partner_sharing(
            tool_ctx, SetPartnerSharingInput(opt_in=True)
        )


async def test_search_messages_hides_partner_raw_until_opt_in(tool_ctx):
    tool_ctx.current_step = "read"
    user_message_id = _seed_message(
        tool_ctx.pool,
        tool_ctx.user.id,
        tool_ctx.partner.id,
        content="user repair phrase",
    )
    partner_message_id = _seed_message(
        tool_ctx.pool,
        tool_ctx.partner.id,
        tool_ctx.user.id,
        content="partner private phrase",
    )

    result = await read_tools.search_messages(
        tool_ctx,
        SearchMessagesInput(text_contains="phrase", limit=10),
    )

    assert [hit.id for hit in result.hits] == [user_message_id]
    assert partner_message_id not in [hit.id for hit in result.hits]

    _set_partner_share(tool_ctx.pool, tool_ctx.partner.id, "mediator", "opt_in")
    result = await read_tools.search_messages(
        tool_ctx,
        SearchMessagesInput(text_contains="phrase", limit=10),
    )

    assert {hit.id for hit in result.hits} == {user_message_id, partner_message_id}


async def test_search_messages_finds_saved_media_explanations(tool_ctx):
    tool_ctx.current_step = "read"
    message_id = _seed_message(
        tool_ctx.pool, tool_ctx.user.id, tool_ctx.partner.id, content=None
    )
    tool_ctx.pool.messages[message_id]["media_type"] = "image"
    tool_ctx.pool.messages[message_id]["media_analysis"] = {
        "kind": "image",
        "explanation": "Screenshot of a calendar showing Hannah's family visit.",
    }

    result = await read_tools.search_messages(
        tool_ctx, SearchMessagesInput(text_contains="family visit", limit=10)
    )

    assert [hit.id for hit in result.hits] == [message_id]
    assert "calendar" in result.hits[0].content


async def test_search_messages_excludes_search_suppressed_rows(tool_ctx):
    tool_ctx.current_step = "read"
    visible_message_id = _seed_message(
        tool_ctx.pool,
        tool_ctx.user.id,
        tool_ctx.partner.id,
        content="needle visible phrase",
    )
    suppressed_message_id = _seed_message(
        tool_ctx.pool,
        tool_ctx.user.id,
        tool_ctx.partner.id,
        content="needle suppressed phrase",
    )
    tool_ctx.pool.messages[suppressed_message_id]["search_suppressed_at"] = datetime.now(UTC)

    result = await read_tools.search_messages(
        tool_ctx,
        SearchMessagesInput(text_contains="needle", limit=10),
    )

    assert [hit.id for hit in result.hits] == [visible_message_id]
    assert suppressed_message_id not in [hit.id for hit in result.hits]


async def test_search_messages_uses_searchable_view_and_partner_thread_filter(tool_ctx):
    tool_ctx.current_step = "read"
    _set_partner_share(tool_ctx.pool, tool_ctx.partner.id, "mediator", "opt_in")
    user_thread_message_id = _seed_message(
        tool_ctx.pool,
        tool_ctx.user.id,
        tool_ctx.partner.id,
        content="shared scoped phrase from user thread",
    )
    partner_thread_message_id = _seed_message(
        tool_ctx.pool,
        tool_ctx.partner.id,
        tool_ctx.user.id,
        content="shared scoped phrase from partner thread",
    )

    result = await read_tools.search_messages(
        tool_ctx,
        SearchMessagesInput(
            partner_user_id=tool_ctx.partner.id,
            text_contains="shared scoped phrase",
            limit=10,
        ),
    )

    assert [hit.id for hit in result.hits] == [partner_thread_message_id]
    assert user_thread_message_id not in [hit.id for hit in result.hits]
    assert any(
        "FROM mediator.v_searchable_messages m" in sql
        for sql in tool_ctx.pool.fetch_sqls
    )


async def test_raw_message_tools_scope_to_current_bot_and_topic(tool_ctx):
    tool_ctx.current_step = "read"
    _set_partner_share(tool_ctx.pool, tool_ctx.partner.id, "mediator", "opt_in")
    _set_partner_share(tool_ctx.pool, tool_ctx.partner.id, "tante_rosi", "opt_in")
    mediator_message_id = _seed_message(
        tool_ctx.pool,
        tool_ctx.partner.id,
        tool_ctx.user.id,
        content="mediator scoped phrase",
        bot_id="mediator",
        topic_id=tool_ctx.primary_topic_id,
    )
    rosi_message_id = _seed_message(
        tool_ctx.pool,
        tool_ctx.partner.id,
        tool_ctx.user.id,
        content="rosi scoped phrase",
        bot_id="tante_rosi",
        topic_id=tool_ctx.primary_topic_id,
    )
    legacy_message_id = _seed_message(
        tool_ctx.pool,
        tool_ctx.partner.id,
        tool_ctx.user.id,
        content="legacy scoped phrase",
        bot_id=None,
        topic_id=tool_ctx.primary_topic_id,
    )
    other_topic_id = _seed_message(
        tool_ctx.pool,
        tool_ctx.partner.id,
        tool_ctx.user.id,
        content="other topic scoped phrase",
        bot_id="mediator",
        topic_id=uuid4(),
    )

    result = await read_tools.search_messages(
        tool_ctx, SearchMessagesInput(text_contains="scoped phrase", limit=10)
    )

    assert [hit.id for hit in result.hits] == [mediator_message_id]
    assert rosi_message_id not in [hit.id for hit in result.hits]
    assert legacy_message_id not in [hit.id for hit in result.hits]
    assert other_topic_id not in [hit.id for hit in result.hits]

    activity = await read_tools.recent_activity(tool_ctx, RecentActivityInput(days=7))
    partner_thread = next(
        thread for thread in activity.threads if thread.user_id == tool_ctx.partner.id
    )
    assert "mediator scoped phrase" in partner_thread.summary
    assert "rosi scoped phrase" not in partner_thread.summary
    assert "legacy scoped phrase" not in partner_thread.summary
    assert "other topic scoped phrase" not in partner_thread.summary


async def test_recent_activity_hides_partner_latest_content_until_opt_in(tool_ctx):
    tool_ctx.current_step = "read"
    _seed_message(
        tool_ctx.pool,
        tool_ctx.partner.id,
        tool_ctx.user.id,
        content="partner private latest",
    )

    hidden = await read_tools.recent_activity(tool_ctx, RecentActivityInput(days=7))
    partner_thread = next(
        thread for thread in hidden.threads if thread.user_id == tool_ctx.partner.id
    )
    assert "partner private latest" not in partner_thread.summary
    assert "hidden by partner_share" in partner_thread.summary

    _set_partner_share(tool_ctx.pool, tool_ctx.partner.id, "mediator", "opt_in")
    visible = await read_tools.recent_activity(tool_ctx, RecentActivityInput(days=7))
    partner_thread = next(
        thread for thread in visible.threads if thread.user_id == tool_ctx.partner.id
    )
    assert "partner private latest" in partner_thread.summary


async def test_bridge_candidate_create_list_update_and_send(tool_ctx, monkeypatch):
    _set_partner_share(tool_ctx.pool, tool_ctx.user.id, tool_ctx.bot_id)
    source_message_id = _seed_message(
        tool_ctx.pool, tool_ctx.user.id, tool_ctx.partner.id
    )
    memory_id = _seed_memory(tool_ctx.pool, tool_ctx.user.id)
    observation_id = _seed_observation(tool_ctx.pool, tool_ctx.user.id)

    created = await write_tools.create_bridge_candidate(
        tool_ctx,
        CreateBridgeCandidateInput(
            source_user_id=tool_ctx.user.id,
            target_user_id=tool_ctx.partner.id,
            kind="repair",
            sensitivity="low",
            partner_path="message_partner",
            source_message_ids=[source_message_id],
            related_memory_ids=[memory_id],
            related_observation_ids=[observation_id],
            internal_note="raw-ish note stays internal",
            shareable_summary="Maya wants to repair this carefully.",
        ),
    )

    assert created.candidate.status == "ready"
    assert created.candidate.partner_path == "message_partner"
    assert created.candidate.source_message_ids == [source_message_id]
    assert tool_ctx.pool.tool_calls[-1]["tool_name"] == "create_bridge_candidate"

    with pytest.raises(write_tools.ToolCallRejected):
        await write_tools.create_bridge_candidate(
            tool_ctx,
            CreateBridgeCandidateInput(
                source_user_id=tool_ctx.partner.id,
                target_user_id=tool_ctx.user.id,
                kind="repair",
                sensitivity="low",
                partner_path="message_partner",
                source_message_ids=[source_message_id],
                shareable_summary="Not allowed from the target side.",
            ),
        )

    read_ctx = TurnContext(
        tool_ctx.turn_id,
        tool_ctx.pool,
        tool_ctx.user,
        tool_ctx.partner,
        [],
        current_step="read",
    )
    listed = await read_tools.list_bridge_candidates(
        read_ctx, ListBridgeCandidatesInput()
    )
    assert listed.candidates[0].internal_note == "raw-ish note stays internal"
    assert listed.candidates[0].partner_path == "message_partner"

    target_ctx = TurnContext(
        tool_ctx.turn_id,
        tool_ctx.pool,
        tool_ctx.partner,
        tool_ctx.user,
        [],
        current_step="read",
    )
    target_listed = await read_tools.list_bridge_candidates(
        target_ctx, ListBridgeCandidatesInput()
    )
    assert (
        target_listed.candidates[0].shareable_summary
        == "Maya wants to repair this carefully."
    )
    assert target_listed.candidates[0].internal_note is None
    assert target_listed.candidates[0].partner_path == "message_partner"

    with pytest.raises(write_tools.ToolCallRejected):
        await write_tools.update_bridge_candidate(
            TurnContext(
                tool_ctx.turn_id,
                tool_ctx.pool,
                tool_ctx.partner,
                tool_ctx.user,
                [],
                current_step="record",
            ),
            UpdateBridgeCandidateInput(
                candidate_id=created.candidate.id,
                status="ready",
                shareable_summary="target must not rewrite source summary",
            ),
        )

    target_addressed = await write_tools.update_bridge_candidate(
        TurnContext(
            tool_ctx.turn_id,
            tool_ctx.pool,
            tool_ctx.partner,
            tool_ctx.user,
            [],
            current_step="record",
        ),
        UpdateBridgeCandidateInput(
            candidate_id=created.candidate.id, status="addressed"
        ),
    )
    assert target_addressed.candidate.status == "addressed"
    assert target_addressed.candidate.internal_note is None

    updated = await write_tools.update_bridge_candidate(
        tool_ctx,
        UpdateBridgeCandidateInput(
            candidate_id=created.candidate.id,
            status="addressed",
            shareable_summary="The repair was addressed.",
        ),
    )
    assert updated.candidate.status == "addressed"
    assert updated.candidate.resolved_at is not None

    ready = await write_tools.create_bridge_candidate(
        tool_ctx,
        CreateBridgeCandidateInput(
            source_user_id=tool_ctx.user.id,
            target_user_id=tool_ctx.partner.id,
            kind="process",
            sensitivity="medium",
            partner_path="message_partner",
            source_message_ids=[source_message_id],
            shareable_summary="Maya can talk after dinner.",
        ),
    )

    sent = []
    sent_message_id = uuid4()
    oob_contexts = []

    async def fake_oob(
        pool, content, recipient_id, protected_owner_ids=None, *, bot_id, topic_id
    ):
        oob_contexts.append((bot_id, topic_id))
        return {
            "verdict": "ok",
            "reason": "ok",
            "suggested_rewrite": None,
            "checker_failed": False,
        }

    async def fake_send(
        pool,
        recipient,
        content,
        *,
        template_fallback=None,
        bot_turn_id=None,
        protected_owner_ids=None,
        scope,
    ):
        sent.append(
            (recipient.id, content, protected_owner_ids, scope.bot_id, scope.topic_id)
        )
        return sent_message_id

    monkeypatch.setattr(write_tools, "_call_oob_hook", fake_oob)
    monkeypatch.setattr(write_tools, "send_outbound", fake_send)

    result = await write_tools.send_bridge_candidate(
        tool_ctx,
        SendBridgeCandidateInput(candidate_id=ready.candidate.id, reason="good moment"),
    )

    assert result.candidate.status == "sent"
    assert result.candidate.sent_message_id == sent_message_id
    assert sent == [
        (
            tool_ctx.partner.id,
            "Maya can talk after dinner.",
            [tool_ctx.user.id, tool_ctx.partner.id],
            tool_ctx.bot_id,
            tool_ctx.primary_topic_id,
        )
    ]
    assert oob_contexts == [(tool_ctx.bot_id, tool_ctx.primary_topic_id)]


async def test_edit_outbound_message_requires_ctx_identity_before_oob(
    tool_ctx, app_env, monkeypatch
):
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    get_settings.cache_clear()
    message_id = _seed_message(
        tool_ctx.pool,
        tool_ctx.user.id,
        tool_ctx.partner.id,
        direction="outbound",
        content="old",
    )
    tool_ctx.pool.messages[message_id]["whatsapp_message_id"] = "discord-outbound-1"
    ctx_without_bot = replace(tool_ctx, bot_id=None)

    async def fail_oob(*args, **kwargs):
        raise AssertionError("_call_oob_hook should not run without ctx.bot_id")

    monkeypatch.setattr(write_tools, "_call_oob_hook", fail_oob)

    with pytest.raises(ValueError, match="missing bot_id"):
        await write_tools.edit_outbound_message(
            ctx_without_bot,
            EditOutboundMessageInput(
                message_id=message_id, content="new", reason="identity regression"
            ),
        )


async def test_bridge_candidate_send_rejects_non_ready_and_blocks_on_oob(
    tool_ctx, monkeypatch
):
    source_message_id = _seed_message(
        tool_ctx.pool, tool_ctx.user.id, tool_ctx.partner.id
    )
    pending = await write_tools.create_bridge_candidate(
        tool_ctx,
        CreateBridgeCandidateInput(
            source_user_id=tool_ctx.user.id,
            target_user_id=tool_ctx.partner.id,
            kind="clarification",
            sensitivity="high",
            partner_path="message_partner",
            source_message_ids=[source_message_id],
            shareable_summary="Sensitive summary.",
        ),
    )

    with pytest.raises(write_tools.ToolCallRejected):
        await write_tools.send_bridge_candidate(
            tool_ctx,
            SendBridgeCandidateInput(candidate_id=pending.candidate.id),
        )

    _set_partner_share(tool_ctx.pool, tool_ctx.user.id, tool_ctx.bot_id)
    ready = await write_tools.create_bridge_candidate(
        tool_ctx,
        CreateBridgeCandidateInput(
            source_user_id=tool_ctx.user.id,
            target_user_id=tool_ctx.partner.id,
            kind="clarification",
            sensitivity="low",
            partner_path="message_partner",
            source_message_ids=[source_message_id],
            shareable_summary="Allowed-looking summary.",
        ),
    )
    sent = []

    async def fake_oob(
        pool, content, recipient_id, protected_owner_ids=None, *, bot_id, topic_id
    ):
        return {
            "verdict": "block",
            "reason": "too revealing",
            "suggested_rewrite": None,
            "checker_failed": False,
        }

    async def fake_send(*args, **kwargs):
        sent.append((args, kwargs))
        return uuid4()

    monkeypatch.setattr(write_tools, "_call_oob_hook", fake_oob)
    monkeypatch.setattr(write_tools, "send_outbound", fake_send)

    blocked = await write_tools.send_bridge_candidate(
        tool_ctx,
        SendBridgeCandidateInput(candidate_id=ready.candidate.id),
    )

    assert blocked.candidate.status == "blocked"
    assert blocked.candidate.partner_path == "message_partner"
    assert "too revealing" in (blocked.candidate.internal_note or "")
    assert blocked.candidate.resolved_at is not None
    assert sent == []


async def test_bridge_candidate_partner_path_persists_and_updates(tool_ctx):
    _set_partner_share(tool_ctx.pool, tool_ctx.user.id, tool_ctx.bot_id)
    source_message_id = _seed_message(
        tool_ctx.pool, tool_ctx.user.id, tool_ctx.partner.id
    )

    created = await write_tools.create_bridge_candidate(
        tool_ctx,
        CreateBridgeCandidateInput(
            source_user_id=tool_ctx.user.id,
            target_user_id=tool_ctx.partner.id,
            kind="process",
            sensitivity="low",
            partner_path="coach_in_person",
            source_message_ids=[source_message_id],
            shareable_summary="Maya should raise this directly.",
        ),
    )

    assert created.candidate.status == "ready"
    assert created.candidate.partner_path == "coach_in_person"

    updated = await write_tools.update_bridge_candidate(
        tool_ctx,
        UpdateBridgeCandidateInput(
            candidate_id=created.candidate.id,
            partner_path="do_not_bridge",
        ),
    )

    assert updated.candidate.partner_path == "do_not_bridge"
    assert updated.candidate.status == "declined"
    assert updated.candidate.resolved_at is not None


async def test_bridge_candidate_target_cannot_change_partner_path(tool_ctx):
    _set_partner_share(tool_ctx.pool, tool_ctx.user.id, tool_ctx.bot_id)
    source_message_id = _seed_message(
        tool_ctx.pool, tool_ctx.user.id, tool_ctx.partner.id
    )
    created = await write_tools.create_bridge_candidate(
        tool_ctx,
        CreateBridgeCandidateInput(
            source_user_id=tool_ctx.user.id,
            target_user_id=tool_ctx.partner.id,
            kind="repair",
            sensitivity="low",
            partner_path="message_partner",
            source_message_ids=[source_message_id],
            shareable_summary="Maya wants this understood.",
        ),
    )

    with pytest.raises(write_tools.ToolCallRejected):
        await write_tools.update_bridge_candidate(
            TurnContext(
                tool_ctx.turn_id,
                tool_ctx.pool,
                tool_ctx.partner,
                tool_ctx.user,
                [],
                current_step="record",
            ),
            UpdateBridgeCandidateInput(
                candidate_id=created.candidate.id,
                status="addressed",
                partner_path="hold_for_context",
            ),
        )


async def test_bridge_candidate_path_locked_after_terminal_status(tool_ctx):
    _set_partner_share(tool_ctx.pool, tool_ctx.user.id, tool_ctx.bot_id)
    source_message_id = _seed_message(
        tool_ctx.pool, tool_ctx.user.id, tool_ctx.partner.id
    )
    created = await write_tools.create_bridge_candidate(
        tool_ctx,
        CreateBridgeCandidateInput(
            source_user_id=tool_ctx.user.id,
            target_user_id=tool_ctx.partner.id,
            kind="clarification",
            sensitivity="low",
            partner_path="message_partner",
            source_message_ids=[source_message_id],
            shareable_summary="Maya clarified the timing.",
        ),
    )
    addressed = await write_tools.update_bridge_candidate(
        tool_ctx,
        UpdateBridgeCandidateInput(
            candidate_id=created.candidate.id, status="addressed"
        ),
    )
    assert addressed.candidate.status == "addressed"

    with pytest.raises(write_tools.ToolCallRejected):
        await write_tools.update_bridge_candidate(
            tool_ctx,
            UpdateBridgeCandidateInput(
                candidate_id=created.candidate.id,
                partner_path="hold_for_context",
            ),
        )


async def test_list_bridge_candidates_hides_non_message_partner_ready_from_target(
    tool_ctx,
):
    _set_partner_share(tool_ctx.pool, tool_ctx.user.id, tool_ctx.bot_id)
    source_message_id = _seed_message(
        tool_ctx.pool, tool_ctx.user.id, tool_ctx.partner.id
    )
    hidden = await write_tools.create_bridge_candidate(
        tool_ctx,
        CreateBridgeCandidateInput(
            source_user_id=tool_ctx.user.id,
            target_user_id=tool_ctx.partner.id,
            kind="context",
            sensitivity="low",
            partner_path="hold_for_context",
            source_message_ids=[source_message_id],
            shareable_summary="Maya is holding this for later context.",
        ),
    )

    source_list = await read_tools.list_bridge_candidates(
        TurnContext(
            tool_ctx.turn_id,
            tool_ctx.pool,
            tool_ctx.user,
            tool_ctx.partner,
            [],
            current_step="read",
        ),
        ListBridgeCandidatesInput(),
    )
    target_list = await read_tools.list_bridge_candidates(
        TurnContext(
            tool_ctx.turn_id,
            tool_ctx.pool,
            tool_ctx.partner,
            tool_ctx.user,
            [],
            current_step="read",
        ),
        ListBridgeCandidatesInput(),
    )

    assert hidden.candidate.id in {candidate.id for candidate in source_list.candidates}
    assert hidden.candidate.partner_path == "hold_for_context"
    assert hidden.candidate.id not in {
        candidate.id for candidate in target_list.candidates
    }


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


async def test_schedule_checkin_rejects_past_time(tool_ctx):
    with pytest.raises(write_tools.ToolCallRejected) as exc_info:
        await write_tools.schedule_checkin(
            tool_ctx,
            ScheduleCheckinInput(
                user_id=tool_ctx.user.id,
                when=datetime.now(UTC) - timedelta(minutes=1),
                about_what="the repair",
                reason="past times should not be accepted",
            ),
        )

    assert exc_info.value.result["error"] == "schedule_time_in_past"


async def test_schedule_checkin_accepts_relative_delay(tool_ctx):
    before = datetime.now(UTC)
    result = await write_tools.schedule_checkin(
        tool_ctx,
        ScheduleCheckinInput(
            user_id=tool_ctx.user.id,
            delay=ScheduleDelay(days=2),
            about_what="the repair",
            reason="relative delay requested",
        ),
    )
    scheduled_for = tool_ctx.pool.scheduled_jobs[result.job_id]["scheduled_for"]

    assert (
        before + timedelta(days=2)
        <= scheduled_for
        <= datetime.now(UTC) + timedelta(days=2, seconds=1)
    )


async def test_schedule_checkin_accepts_local_berlin_clock_time(tool_ctx):
    berlin_user = User(
        tool_ctx.user.id, tool_ctx.user.name, tool_ctx.user.phone, "Europe/Berlin"
    )
    berlin_ctx = TurnContext(
        tool_ctx.turn_id,
        tool_ctx.pool,
        berlin_user,
        tool_ctx.partner,
        tool_ctx.triggering_message_ids,
        current_step=tool_ctx.current_step,
    )

    exact_2026_conversion = write_tools._local_when_to_utc(
        berlin_ctx,
        LocalScheduleTime(date=date(2026, 5, 6), time=time(21, 0)),
    )
    assert exact_2026_conversion == datetime(2026, 5, 6, 19, 0, tzinfo=UTC)

    result = await write_tools.schedule_checkin(
        berlin_ctx,
        ScheduleCheckinInput(
            user_id=berlin_user.id,
            local_when=LocalScheduleTime(date=date(2036, 5, 6), time=time(21, 0)),
            about_what="the 9pm conversation",
            reason="user asked for a check-in at local 9pm",
        ),
    )

    assert tool_ctx.pool.scheduled_jobs[result.job_id]["scheduled_for"] == datetime(
        2036, 5, 6, 19, 0, tzinfo=UTC
    )


async def test_schedule_checkin_rejects_utc_when_for_non_utc_user(tool_ctx):
    berlin_user = User(
        tool_ctx.user.id, tool_ctx.user.name, tool_ctx.user.phone, "Europe/Berlin"
    )
    berlin_ctx = TurnContext(
        tool_ctx.turn_id,
        tool_ctx.pool,
        berlin_user,
        tool_ctx.partner,
        tool_ctx.triggering_message_ids,
        current_step=tool_ctx.current_step,
    )

    with pytest.raises(write_tools.ToolCallRejected) as exc:
        await write_tools.schedule_checkin(
            berlin_ctx,
            ScheduleCheckinInput(
                user_id=berlin_user.id,
                when=datetime(2036, 5, 6, 21, 0, tzinfo=UTC),
                about_what="the 9pm conversation",
                reason="user asked for a check-in at local 9pm",
            ),
        )

    assert exc.value.result["error"] == "use_local_when_for_user_local_time"
    assert exc.value.result["timezone"] == "Europe/Berlin"


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
            bot_id=tool_ctx.bot_id,
            topic_id=tool_ctx.primary_topic_id,
        )


async def test_schedule_checkin_job_shares_utc_supersede_path(tool_ctx):
    old_id = _seed_job(tool_ctx.pool, tool_ctx.user.id)
    plus_two = timezone(timedelta(hours=2))

    row = await schedule_checkin_job(
        tool_ctx.pool,
        tool_ctx.user.id,
        scheduled_for=datetime(2026, 5, 1, 9, 30, tzinfo=plus_two),
        context={"about_what": "handler path", "reason": "shared helper"},
        bot_id=tool_ctx.bot_id,
        topic_id=tool_ctx.primary_topic_id,
    )

    assert tool_ctx.pool.scheduled_jobs[old_id]["status"] == "superseded"
    assert tool_ctx.pool.scheduled_jobs[row["job_id"]]["scheduled_for"] == datetime(
        2026, 5, 1, 7, 30, tzinfo=UTC
    )


async def test_watch_item_due_schedules_due_job(tool_ctx):
    due_at = datetime.now(UTC) + timedelta(days=2)

    result = await write_tools.add_watch_item(
        tool_ctx,
        AddWatchItemInput(
            owner_user_id=tool_ctx.user.id,
            content="check after the talk",
            due_at=due_at,
        ),
    )

    jobs = [
        job
        for job in tool_ctx.pool.scheduled_jobs.values()
        if job["job_type"] == "watch_item_due"
    ]
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

    jobs = [
        job
        for job in tool_ctx.pool.scheduled_jobs.values()
        if job["job_type"] == "oob_review"
    ]
    assert len(jobs) == 1
    assert jobs[0]["user_id"] == tool_ctx.user.id
    assert jobs[0]["scheduled_for"] == review_at
    assert jobs[0]["context"]["oob_id"] == str(result.id)


async def test_check_oob_read_tool_passes_protected_owner_ids(tool_ctx, monkeypatch):
    calls = []

    async def fake_check_oob_with_policy(
        pool,
        *,
        content,
        recipient_id,
        protected_owner_ids=None,
        sender_intent=None,
        topic_id=None,
    ):
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
    assert calls == [
        (tool_ctx.pool, "draft", tool_ctx.partner.id, protected_owner_ids, "relay")
    ]


async def test_consult_phase_check_oob_inherits_protected_owner_ids(
    tool_ctx, monkeypatch
):
    calls = []

    async def fake_check_oob_with_policy(
        pool,
        *,
        content,
        recipient_id,
        protected_owner_ids=None,
        sender_intent=None,
        topic_id=None,
    ):
        calls.append(protected_owner_ids)
        return {
            "verdict": "ok",
            "reason": "checked",
            "triggering_oob_ids": [],
            "suggested_rewrite": None,
            "checker_failed": False,
        }

    monkeypatch.setattr(read_tools, "check_oob_with_policy", fake_check_oob_with_policy)
    tool_ctx.current_step = "consult"
    tool_ctx.protected_owner_ids = [tool_ctx.user.id, tool_ctx.partner.id]

    result = await call_tool(
        "check_oob",
        {"content": "draft", "recipient_id": str(tool_ctx.partner.id)},
        tool_ctx,
    )

    assert result["verdict"] == "ok"
    assert calls == [[tool_ctx.user.id, tool_ctx.partner.id]]


async def test_consult_phase_check_oob_unions_partial_protected_owner_ids(
    tool_ctx, monkeypatch
):
    calls = []
    extra_owner_id = uuid4()

    async def fake_check_oob_with_policy(
        pool,
        *,
        content,
        recipient_id,
        protected_owner_ids=None,
        sender_intent=None,
        topic_id=None,
    ):
        calls.append(protected_owner_ids)
        return {
            "verdict": "ok",
            "reason": "checked",
            "triggering_oob_ids": [],
            "suggested_rewrite": None,
            "checker_failed": False,
        }

    monkeypatch.setattr(read_tools, "check_oob_with_policy", fake_check_oob_with_policy)
    tool_ctx.current_step = "consult"
    tool_ctx.protected_owner_ids = [tool_ctx.user.id, tool_ctx.partner.id]

    result = await call_tool(
        "check_oob",
        {
            "content": "draft",
            "recipient_id": str(tool_ctx.partner.id),
            "protected_owner_ids": [str(extra_owner_id), str(tool_ctx.user.id)],
        },
        tool_ctx,
    )

    assert result["verdict"] == "ok"
    assert calls == [[extra_owner_id, tool_ctx.user.id, tool_ctx.partner.id]]


async def test_read_phase_check_oob_does_not_inject_protected_owner_ids(
    tool_ctx, monkeypatch
):
    calls = []

    async def fake_check_oob_with_policy(
        pool,
        *,
        content,
        recipient_id,
        protected_owner_ids=None,
        sender_intent=None,
        topic_id=None,
    ):
        calls.append(protected_owner_ids)
        return {
            "verdict": "ok",
            "reason": "checked",
            "triggering_oob_ids": [],
            "suggested_rewrite": None,
            "checker_failed": False,
        }

    monkeypatch.setattr(read_tools, "check_oob_with_policy", fake_check_oob_with_policy)
    tool_ctx.current_step = "read"
    tool_ctx.protected_owner_ids = [tool_ctx.user.id, tool_ctx.partner.id]

    result = await call_tool(
        "check_oob",
        {"content": "draft", "recipient_id": str(tool_ctx.partner.id)},
        tool_ctx,
    )

    assert result["verdict"] == "ok"
    assert calls == [None]


async def test_search_emojis_returns_precise_candidates(tool_ctx):
    result = await read_tools.search_emojis(
        tool_ctx, SearchEmojisInput(query="candle", limit=5)
    )

    assert any(hit.emoji.startswith("🕯") or hit.name == "candle" for hit in result.hits)
    assert result.query == "candle"


async def test_search_emojis_handles_meaning_queries(tool_ctx):
    result = await read_tools.search_emojis(
        tool_ctx, SearchEmojisInput(query="quiet support", limit=8)
    )

    assert result.hits
    assert any(hit.emoji in {"🫶", "🕯️", "🛟", "🤲"} for hit in result.hits)


async def test_escalate_to_partner_passes_dyad_protected_owner_ids(
    tool_ctx, monkeypatch
):
    sent = []
    tool_ctx.trigger_charge = "crisis"

    async def fake_send(
        pool,
        recipient,
        content,
        *,
        template_fallback=None,
        bot_turn_id=None,
        protected_owner_ids=None,
        scope,
    ):
        assert scope.bot_id == tool_ctx.bot_id
        assert scope.topic_id == tool_ctx.primary_topic_id
        sent.append(
            (recipient, content, template_fallback, bot_turn_id, protected_owner_ids)
        )
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
        AddressWatchItemInput(
            watch_item_id=_seed_watch(tool_ctx.pool, tool_ctx.user.id),
            addressing_note="handled",
        ),
    )
    lifted = await write_tools.lift_oob(
        tool_ctx, LiftOOBInput(oob_id=_seed_oob(tool_ctx.pool, tool_ctx.user.id))
    )

    assert style.user_id == tool_ctx.user.id and style.updated_at
    assert addressed.id and addressed.addressed_at
    assert lifted.id and lifted.lifted_at


async def test_log_observation_scores_when_significance_omitted(tool_ctx, monkeypatch):
    async def fake_score(pool, *, content, client=None):
        return 4, "material pattern", "v1"

    monkeypatch.setattr(write_tools.scoring, "score_observation", fake_score)
    result = await write_tools.log_observation(
        tool_ctx,
        LogObservationInput(
            content="obs", about_user_id=tool_ctx.user.id, confidence=Confidence.medium
        ),
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
        LogObservationInput(
            content="obs", about_user_id=tool_ctx.user.id, confidence=Confidence.medium
        ),
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
        LogObservationInput(
            content="obs",
            about_user_id=tool_ctx.user.id,
            confidence=Confidence.medium,
            significance=5,
        ),
    )

    row = tool_ctx.pool.observations[result.id]
    assert row["significance"] == 5
    assert row["scoring_prompt_version"] == "v1"
    assert called is False


async def test_call_tool_validation_error_is_typed(tool_ctx):
    result = await call_tool(
        "log_observation",
        {
            "content": "obs",
            "about_user_id": None,
            "confidence": "medium",
            "significance": 7,
        },
        tool_ctx,
    )

    assert result["is_error"] is True
    assert result["error"].startswith("validation:")


async def test_escalation_gate_rejects_before_outbound_and_allows_crisis(
    tool_ctx, monkeypatch
):
    sent = []

    async def fake_send(
        pool,
        recipient,
        content,
        *,
        template_fallback=None,
        bot_turn_id=None,
        protected_owner_ids=None,
        scope,
    ):
        assert scope.bot_id == tool_ctx.bot_id
        assert scope.topic_id == tool_ctx.primary_topic_id
        sent.append(
            (recipient, content, template_fallback, bot_turn_id, protected_owner_ids)
        )
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
    assert (
        "ESCALATION_SENT gate=crisis"
        in tool_ctx.pool.bot_turns[tool_ctx.turn_id]["reasoning"]
    )


async def test_escalation_allows_trusted_explicit_partner_alert(tool_ctx, monkeypatch):
    sent = []

    async def fake_send(
        pool,
        recipient,
        content,
        *,
        template_fallback=None,
        bot_turn_id=None,
        protected_owner_ids=None,
        scope,
    ):
        assert scope.bot_id == tool_ctx.bot_id
        assert scope.topic_id == tool_ctx.primary_topic_id
        sent.append(
            (recipient, content, template_fallback, bot_turn_id, protected_owner_ids)
        )
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
    assert (
        "ESCALATION_SENT gate=explicit_partner_alert"
        in tool_ctx.pool.bot_turns[tool_ctx.turn_id]["reasoning"]
    )


async def test_step_enforcement_returns_typed_errors(tool_ctx):
    tool_ctx.current_step = "read"
    write_result = await call_tool(
        "add_memory", {"about_user_id": str(tool_ctx.user.id), "content": "x"}, tool_ctx
    )
    tool_ctx.current_step = "schedule"
    read_result = await call_tool(
        "get_memories", {"about_user_id": str(tool_ctx.user.id)}, tool_ctx
    )

    assert write_result["is_error"] is True and write_result["error"].startswith(
        "step:"
    )
    assert read_result["is_error"] is True and read_result["error"].startswith("step:")


async def test_recent_activity_returns_period_and_stub_digest(tool_ctx):
    tool_ctx.current_step = "read"
    sent_at = datetime.now(UTC) - timedelta(minutes=5)
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
        "bot_id": tool_ctx.bot_id,
        "topic_id": tool_ctx.primary_topic_id,
    }

    result = await call_tool(
        "recent_activity", RecentActivityInput(days=7).model_dump(mode="json"), tool_ctx
    )

    assert "error" not in result
    assert result["period"]["start"] is not None
    assert result["period"]["end"] is not None
    user_thread = next(
        thread
        for thread in result["threads"]
        if thread["user_id"] == str(tool_ctx.user.id)
    )
    assert user_thread["message_count"] == 1
    assert user_thread["summary"] == '1 messages this period; latest: "latest context"'
    assert user_thread["last_message_at_time"]["relative_to_now"].endswith("ago")
    assert user_thread["last_message_at_time"]["local_day_label"] == "today"
    assert result["period_time"]["start"]["display"]


async def test_recent_activity_handles_solo_context(tool_ctx):
    tool_ctx.current_step = "read"
    tool_ctx.partner = None

    result = await call_tool(
        "recent_activity", RecentActivityInput(days=7).model_dump(mode="json"), tool_ctx
    )

    assert "error" not in result
    assert [thread["user_id"] for thread in result["threads"]] == [str(tool_ctx.user.id)]


async def test_search_messages_returns_temporal_metadata_and_local_day_filter(tool_ctx):
    tool_ctx.current_step = "read"
    tool_ctx.user = replace(tool_ctx.user, timezone="Europe/Berlin")
    tool_ctx.turn_started_at = datetime(2026, 5, 6, 22, 30, tzinfo=UTC)
    included_id = uuid4()
    excluded_id = uuid4()
    tool_ctx.pool.messages[included_id] = {
        "id": included_id,
        "direction": "inbound",
        "sender_id": tool_ctx.user.id,
        "recipient_id": None,
        "content": "after Berlin midnight",
        "processing_state": "processed",
        "sent_at": datetime(2026, 5, 6, 22, 15, tzinfo=UTC),
        "charge": "routine",
        "deleted_at": None,
        "bot_id": tool_ctx.bot_id,
        "topic_id": tool_ctx.primary_topic_id,
    }
    tool_ctx.pool.messages[excluded_id] = {
        "id": excluded_id,
        "direction": "inbound",
        "sender_id": tool_ctx.user.id,
        "recipient_id": None,
        "content": "before Berlin midnight",
        "processing_state": "processed",
        "sent_at": datetime(2026, 5, 6, 21, 30, tzinfo=UTC),
        "charge": "routine",
        "deleted_at": None,
        "bot_id": tool_ctx.bot_id,
        "topic_id": tool_ctx.primary_topic_id,
    }

    result = await call_tool(
        "search_messages",
        {"local_day": "today", "text_contains": "Berlin midnight"},
        tool_ctx,
    )

    assert "error" not in result
    assert [hit["id"] for hit in result["hits"]] == [str(included_id)]
    hit_time = result["hits"][0]["sent_at_time"]
    assert hit_time["display"] == "today 00:15 Berlin"
    assert hit_time["relative_to_now"] == "about 15 minutes ago"
    assert hit_time["utc"] == "2026-05-06T22:15:00+00:00"


async def test_get_bot_actions_includes_trigger_and_outbound_content(tool_ctx):
    tool_ctx.current_step = "read"
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
        "bot_id": tool_ctx.bot_id,
        "topic_id": tool_ctx.primary_topic_id,
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
        "bot_id": tool_ctx.bot_id,
        "topic_id": tool_ctx.primary_topic_id,
    }
    tool_ctx.pool.bot_turns[tool_ctx.turn_id].update(
        started_at=datetime.now(UTC),
        user_in_context=tool_ctx.user.id,
        triggered_by_message_id=inbound_id,
        final_output_message_id=outbound_id,
        reasoning="explicit request",
        triggering_message_ids=[inbound_id],
    )
    tool_ctx.pool.tool_calls.append(
        {
            "turn_id": tool_ctx.turn_id,
            "tool_name": "escalate_to_partner",
            "arguments": {},
            "result": {},
            "called_at": datetime.now(UTC),
            "duration_ms": 1,
        }
    )
    tool_ctx.pool.turn_audit_events.append(
        {
            "id": uuid4(),
            "turn_id": tool_ctx.turn_id,
            "event_seq": 1,
            "event_type": "outbound.sent",
            "step": "respond",
            "severity": "info",
            "occurred_at": datetime.now(UTC).isoformat(),
            "duration_ms": 2,
            "actor": "delivery",
            "message": None,
            "metadata": {"message_id": outbound_id},
            "sensitive_metadata_encrypted": b"raw ciphertext",
        }
    )

    result = await call_tool("get_bot_actions", {"target_type": "escalation"}, tool_ctx)

    action = result["actions"][0]
    assert action["triggering_content"] == "why did you tell her that?"
    assert action["final_outbound_content"] == "because you asked me to"
    assert action["tool_calls"][0]["tool_name"] == "escalate_to_partner"
    assert any(
        event["event_type"] == "outbound.sent" for event in action["audit_events"]
    )
    outbound_event = next(
        event for event in action["audit_events"] if event["event_type"] == "outbound.sent"
    )
    assert outbound_event["occurred_at_time"]["utc"].endswith("+00:00")
    assert "raw ciphertext" not in str(action["audit_events"])


async def test_get_bot_actions_filters_distillation_target(tool_ctx):
    tool_ctx.current_step = "read"
    tool_ctx.pool.bot_turns[tool_ctx.turn_id].update(
        started_at=datetime.now(UTC),
        user_in_context=tool_ctx.user.id,
        triggered_by_message_id=None,
        final_output_message_id=None,
        reasoning="distillation audit",
    )
    tool_ctx.pool.tool_calls.append(
        {
            "turn_id": tool_ctx.turn_id,
            "tool_name": "revise_distillation",
            "arguments": {},
            "result": {},
            "called_at": datetime.now(UTC),
            "duration_ms": 1,
        }
    )

    result = await call_tool(
        "get_bot_actions", {"target_type": "distillation"}, tool_ctx
    )

    assert result["actions"][0]["tool_calls"][0]["tool_name"] == "revise_distillation"


# ---------------------------------------------------------------------------
# Tool validation error → model-visible dict (regression for incident)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_validation_error_is_typed(fake_pool):
    """call_tool must return a model-visible dict (is_error=True), not raise an uncaught exception."""
    from uuid import uuid4

    from app.services.turn_context import TurnContext
    from app.services.tools.registry import call_tool
    from app.models.user import User
    from app.bots.registry import get_relationship_topic_id

    user = User(uuid4(), "ValidationTest", "15555550199", "UTC")
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
        "onboarding_state": "welcomed",
        "pacing_preferences": {},
        "pregnancy_edd": None,
        "pregnancy_dating_basis": None,
        "pregnancy_lmp_date": None,
        "pregnancy_scan_date": None,
        "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": None,
        "pregnancy_ended_at": None,
        "pregnancy_outcome": None,
    }
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "reasoning": "",
        "completed_at": None,
        "failure_reason": None,
    }
    # Use Hector bot + fitness topic so the Hector-specific tools are allowed
    fitness_topic_id = uuid4()
    ctx = TurnContext(
        turn_id=turn_id,
        pool=fake_pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id="hector",
        primary_topic_id=fitness_topic_id,
        primary_topic_slug="fitness",
        current_step="record",
    )

    result = await call_tool(
        "log_event",
        {
            "commitment_id": "pending",
            "metric_key": "test",
            "adherence_status": "done",
        },
        ctx,
    )

    assert isinstance(result, dict), "call_tool must return a dict for the model"
    assert result["is_error"] is True, "placeholder ID must be a model-visible error"
    assert result["error_code"] == "invalid_uuid"
    assert result["field"] == "commitment_id"
    assert result["retryable"] is True
    assert "list_commitments" in result["correction_hint"].lower()
    assert "create_commitment" in result["correction_hint"].lower()

    # The call must NOT crash — we got here, so the dict path worked.
    # Also verify no SQL was executed against the events table.
    assert len(fake_pool.events) == 0
