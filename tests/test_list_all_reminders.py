"""Tests for ``list_all_reminders`` (SD-013).

Covers: returns both kinds scoped and sorted ascending; recurrence_label for
all enumerated forms; recurrence_rule is the canonical normalized dict;
DST-boundary fixture; NULL context guard; scope isolation; cancelled/fired
exclusion; non-task/non-checkin job_type exclusion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services.tools import read_tools
from app.services.turn_context import TurnContext
from tool_schemas import ListAllRemindersInput

pytestmark = pytest.mark.anyio

# ── helpers ────────────────────────────────────────────────────────────────


def _build_ctx(fake_pool, user, *, bot_id):
    fake_pool.users.setdefault(
        user.id,
        {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
        },
    )
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
        None,
        [uuid4()],
        current_step="read",
        bot_id=bot_id,
        user_id=user.id,
        primary_topic_id=get_relationship_topic_id(),
    )


def _seed_job(pool, *, job_type, user_id, bot_id, topic_id, scheduled_for,
              context=None, status="pending"):
    job_id = uuid4()
    pool.scheduled_jobs[job_id] = {
        "id": job_id,
        "user_id": user_id,
        "job_type": job_type,
        "scheduled_for": scheduled_for,
        "context": context or {},
        "status": status,
        "bot_id": bot_id,
        "topic_id": topic_id,
        "created_at": datetime.now(UTC),
    }
    return job_id


# ── basic shape / scoping ───────────────────────────────────────────────────


async def test_returns_both_kinds_sorted_ascending(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    t1 = _seed_job(
        fake_pool, job_type="scheduled_task", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=2),
        context={"brief": "task-2h"},
    )
    c1 = _seed_job(
        fake_pool, job_type="checkin", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=1),
        context={"about_what": "checkin-1h"},
    )
    t2 = _seed_job(
        fake_pool, job_type="scheduled_task", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=3),
        context={"brief": "task-3h"},
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 3
    # Ascending order
    assert result.items[0].kind == "checkin"
    assert result.items[0].id == c1
    assert result.items[1].kind == "task"
    assert result.items[1].id == t1
    assert result.items[2].kind == "task"
    assert result.items[2].id == t2


async def test_scope_isolation_other_user(fake_pool):
    user_a = User(uuid4(), "Maya", "15555550100", "UTC")
    user_b = User(uuid4(), "Ben", "15555550101", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    _seed_job(
        fake_pool, job_type="checkin", user_id=user_b.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=1),
        context={"about_what": "ben-checkin"},
    )
    ctx = _build_ctx(fake_pool, user_a, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 0


async def test_scope_isolation_other_bot(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    _seed_job(
        fake_pool, job_type="checkin", user_id=user.id, bot_id="mediator",
        topic_id=tid, scheduled_for=now + timedelta(hours=1),
        context={"about_what": "mediator-checkin"},
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 0


async def test_scope_isolation_other_topic(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    other_tid = uuid4()
    now = datetime.now(UTC)
    _seed_job(
        fake_pool, job_type="checkin", user_id=user.id, bot_id="hector",
        topic_id=other_tid, scheduled_for=now + timedelta(hours=1),
        context={"about_what": "other-topic"},
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 0


# ── status filtering ────────────────────────────────────────────────────────


async def test_excludes_cancelled_rows(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    jid = _seed_job(
        fake_pool, job_type="checkin", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=1),
        context={"about_what": "cancelled"},
    )
    fake_pool.scheduled_jobs[jid]["status"] = "cancelled"
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 0


async def test_excludes_fired_rows(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    jid = _seed_job(
        fake_pool, job_type="checkin", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=1),
        context={"about_what": "fired"},
    )
    fake_pool.scheduled_jobs[jid]["status"] = "fired"
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 0


# ── job_type filtering ──────────────────────────────────────────────────────


async def test_excludes_heartbeat_rows(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    _seed_job(
        fake_pool, job_type="heartbeat", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=1),
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 0


async def test_excludes_watch_item_due_rows(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    _seed_job(
        fake_pool, job_type="watch_item_due", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=1),
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 0


async def test_excludes_oob_review_rows(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    _seed_job(
        fake_pool, job_type="oob_review", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=1),
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 0


# ── NULL context guard ──────────────────────────────────────────────────────


async def test_null_context_does_not_crash(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    jid = _seed_job(
        fake_pool, job_type="checkin", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=1),
        context={"about_what": "has-context"},
    )
    # Simulate a NULL context column
    fake_pool.scheduled_jobs[jid]["context"] = None
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    assert result.items[0].about_what is None
    assert result.items[0].brief is None


# ── recurrence_label ────────────────────────────────────────────────────────


async def test_recurrence_label_one_off(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    _seed_job(
        fake_pool, job_type="scheduled_task", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=1),
        context={"brief": "no-recurrence"},
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    assert result.items[0].recurrence_label == "one-off"
    assert result.items[0].recurrence_rule is None


async def test_recurrence_label_daily(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    sf = datetime(2026, 5, 19, 9, 0, tzinfo=UTC)
    _seed_job(
        fake_pool, job_type="scheduled_task", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=sf,
        context={
            "brief": "daily-task",
            "recurrence": {"version": 1, "type": "daily", "interval": 1},
        },
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    assert "daily at" in result.items[0].recurrence_label
    assert result.items[0].recurrence_rule == {"version": 1, "type": "daily", "interval": 1}


async def test_recurrence_label_daily_n(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    sf = datetime(2026, 5, 19, 14, 0, tzinfo=UTC)
    _seed_job(
        fake_pool, job_type="scheduled_task", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=sf,
        context={
            "brief": "every-3-days",
            "recurrence": {"version": 1, "type": "daily", "interval": 3},
        },
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    assert result.items[0].recurrence_label.startswith("every 3 days at")


async def test_recurrence_label_weekly_single(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    # Monday = 0 in the recurrence schema
    sf = datetime(2026, 5, 18, 8, 30, tzinfo=UTC)  # Monday
    _seed_job(
        fake_pool, job_type="scheduled_task", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=sf,
        context={
            "brief": "weekly-monday",
            "recurrence": {"version": 1, "type": "weekly", "interval": 1,
                           "weekdays": [0]},
        },
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    assert result.items[0].recurrence_label.startswith("weekly Mon")


async def test_recurrence_label_weekly_multi(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    sf = datetime(2026, 5, 19, 10, 0, tzinfo=UTC)
    _seed_job(
        fake_pool, job_type="scheduled_task", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=sf,
        context={
            "brief": "mon-wed-fri",
            "recurrence": {"version": 1, "type": "weekly", "interval": 1,
                           "weekdays": [0, 2, 4]},
        },
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    label = result.items[0].recurrence_label
    assert "weekly" in label
    assert "Mon" in label
    assert "Wed" in label
    assert "Fri" in label
    assert "+" in label


async def test_recurrence_label_weekly_n(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    sf = datetime(2026, 5, 19, 10, 0, tzinfo=UTC)
    _seed_job(
        fake_pool, job_type="scheduled_task", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=sf,
        context={
            "brief": "every-2-weeks",
            "recurrence": {"version": 1, "type": "weekly", "interval": 2,
                           "weekdays": [2]},
        },
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    assert result.items[0].recurrence_label.startswith("every 2 weeks")


async def test_recurrence_label_hourly(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    sf = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
    _seed_job(
        fake_pool, job_type="scheduled_task", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=sf,
        context={
            "brief": "hourly-check",
            "recurrence": {"version": 1, "type": "hourly", "interval": 1},
        },
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    assert result.items[0].recurrence_label == "hourly"


async def test_recurrence_label_hourly_n(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    sf = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
    _seed_job(
        fake_pool, job_type="scheduled_task", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=sf,
        context={
            "brief": "every-4h",
            "recurrence": {"version": 1, "type": "hourly", "interval": 4},
        },
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    assert result.items[0].recurrence_label.startswith("every 4 hours")


async def test_recurrence_label_fallback_unknown_rule(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    sf = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
    _seed_job(
        fake_pool, job_type="scheduled_task", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=sf,
        context={
            "brief": "unknown-rule",
            "recurrence": {"version": 1, "type": "custom", "interval": 1,
                           "custom_spec": "something new"},
        },
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    # Fallback should not raise and should contain something
    assert result.items[0].recurrence_label
    assert result.items[0].recurrence_rule is not None


async def test_checkin_recurrence_label_is_one_off(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    _seed_job(
        fake_pool, job_type="checkin", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=1),
        context={"about_what": "checkin-msg"},
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    assert result.items[0].recurrence_label == "one-off"
    assert result.items[0].recurrence_rule is None


# ── DST boundary ─────────────────────────────────────────────────────────────


async def test_dst_spring_forward_does_not_raise(fake_pool):
    """Spring-forward week: local time that doesn't exist in wall clock."""
    user = User(uuid4(), "Maya", "15555550100", "America/New_York")
    tid = get_relationship_topic_id()
    # 2026-03-08 02:30 EST → 03:30 EDT (spring forward).  This UTC instant
    # is 2026-03-08 07:30:00 UTC, which in America/New_York is 02:30 EST.
    # After spring forward it becomes 03:30 EDT, but the label generator
    # should not raise.
    sf = datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    _seed_job(
        fake_pool, job_type="checkin", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=sf,
        context={"about_what": "spring-forward-checkin"},
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    # Should not raise; label should exist
    assert result.items[0].recurrence_label


# ── checkin-specific fields ─────────────────────────────────────────────────


async def test_checkin_has_about_what_not_brief(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    _seed_job(
        fake_pool, job_type="checkin", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=1),
        context={"about_what": "hello user", "reason": "test reason"},
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    assert result.items[0].about_what == "hello user"
    assert result.items[0].brief is None
    assert result.items[0].reason == "test reason"


async def test_task_has_brief_not_about_what(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    _seed_job(
        fake_pool, job_type="scheduled_task", user_id=user.id, bot_id="hector",
        topic_id=tid, scheduled_for=now + timedelta(hours=1),
        context={"brief": "agent task brief"},
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await read_tools.list_all_reminders(ctx, ListAllRemindersInput())
    assert len(result.items) == 1
    assert result.items[0].brief == "agent task brief"
    assert result.items[0].about_what is None
