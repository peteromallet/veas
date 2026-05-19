"""Tests for ``update_scheduled_checkin`` (SD-013).

Covers: time-only update, about_what-only update, reason-only update,
all-combinations; noop on scheduled_task rows; noop on cancelled/fired rows;
noop on other-user rows; noop on other-bot/topic rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services.tools import write_tools
from app.services.turn_context import TurnContext
from tool_schemas import UpdateScheduledCheckinInput

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
        current_step="write",
        bot_id=bot_id,
        user_id=user.id,
        primary_topic_id=get_relationship_topic_id(),
    )


def _seed_checkin(pool, *, user_id, bot_id, topic_id, scheduled_for,
                  about_what=None, reason=None, status="pending"):
    job_id = uuid4()
    pool.scheduled_jobs[job_id] = {
        "id": job_id,
        "user_id": user_id,
        "job_type": "checkin",
        "scheduled_for": scheduled_for,
        "context": {
            "about_what": about_what,
            "reason": reason or "test reason",
        },
        "status": status,
        "bot_id": bot_id,
        "topic_id": topic_id,
        "created_at": datetime.now(UTC),
    }
    return job_id


def _seed_task(pool, *, user_id, bot_id, topic_id, scheduled_for,
               brief=None, status="pending"):
    job_id = uuid4()
    pool.scheduled_jobs[job_id] = {
        "id": job_id,
        "user_id": user_id,
        "job_type": "scheduled_task",
        "scheduled_for": scheduled_for,
        "context": {"brief": brief},
        "status": status,
        "bot_id": bot_id,
        "topic_id": topic_id,
        "created_at": datetime.now(UTC),
    }
    return job_id


# ── happy path ──────────────────────────────────────────────────────────────


async def test_update_time_only(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    original = now + timedelta(hours=1)
    new_time = now + timedelta(hours=5)
    jid = _seed_checkin(
        fake_pool, user_id=user.id, bot_id="hector", topic_id=tid,
        scheduled_for=original, about_what="test checkin",
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await write_tools.update_scheduled_checkin(
        ctx,
        UpdateScheduledCheckinInput(
            job_id=jid,
            when=new_time,
        ),
    )
    assert result.action == "updated"
    assert result.job_id == jid
    assert result.scheduled_for == new_time
    assert result.about_what == "test checkin"


async def test_update_about_what_only(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    jid = _seed_checkin(
        fake_pool, user_id=user.id, bot_id="hector", topic_id=tid,
        scheduled_for=now + timedelta(hours=1), about_what="old message",
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await write_tools.update_scheduled_checkin(
        ctx,
        UpdateScheduledCheckinInput(
            job_id=jid,
            about_what="new message",
        ),
    )
    assert result.action == "updated"
    assert result.about_what == "new message"


async def test_update_reason_only(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    jid = _seed_checkin(
        fake_pool, user_id=user.id, bot_id="hector", topic_id=tid,
        scheduled_for=now + timedelta(hours=1), about_what="checkin",
        reason="old reason",
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await write_tools.update_scheduled_checkin(
        ctx,
        UpdateScheduledCheckinInput(
            job_id=jid,
            reason="new reason",
        ),
    )
    assert result.action == "updated"


async def test_update_all_fields(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    new_time = now + timedelta(hours=10)
    jid = _seed_checkin(
        fake_pool, user_id=user.id, bot_id="hector", topic_id=tid,
        scheduled_for=now + timedelta(hours=1), about_what="old",
        reason="old reason",
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await write_tools.update_scheduled_checkin(
        ctx,
        UpdateScheduledCheckinInput(
            job_id=jid,
            when=new_time,
            about_what="new msg",
            reason="new reason",
        ),
    )
    assert result.action == "updated"
    assert result.scheduled_for == new_time
    assert result.about_what == "new msg"


# ── noop on non-matching rows ───────────────────────────────────────────────


async def test_noop_on_scheduled_task_row(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    jid = _seed_task(
        fake_pool, user_id=user.id, bot_id="hector", topic_id=tid,
        scheduled_for=now + timedelta(hours=1), brief="task brief",
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await write_tools.update_scheduled_checkin(
        ctx,
        UpdateScheduledCheckinInput(
            job_id=jid,
            about_what="should not apply",
        ),
    )
    assert result.action == "noop"


async def test_noop_on_cancelled_row(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    jid = _seed_checkin(
        fake_pool, user_id=user.id, bot_id="hector", topic_id=tid,
        scheduled_for=now + timedelta(hours=1), about_what="cancelled",
        status="cancelled",
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await write_tools.update_scheduled_checkin(
        ctx,
        UpdateScheduledCheckinInput(
            job_id=jid,
            about_what="should not apply",
        ),
    )
    assert result.action == "noop"


async def test_noop_on_fired_row(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    jid = _seed_checkin(
        fake_pool, user_id=user.id, bot_id="hector", topic_id=tid,
        scheduled_for=now + timedelta(hours=1), about_what="fired",
        status="fired",
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await write_tools.update_scheduled_checkin(
        ctx,
        UpdateScheduledCheckinInput(
            job_id=jid,
            about_what="should not apply",
        ),
    )
    assert result.action == "noop"


async def test_noop_on_other_user_row(fake_pool):
    user_a = User(uuid4(), "Maya", "15555550100", "UTC")
    user_b = User(uuid4(), "Ben", "15555550101", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    jid = _seed_checkin(
        fake_pool, user_id=user_b.id, bot_id="hector", topic_id=tid,
        scheduled_for=now + timedelta(hours=1), about_what="ben-checkin",
    )
    ctx = _build_ctx(fake_pool, user_a, bot_id="hector")
    result = await write_tools.update_scheduled_checkin(
        ctx,
        UpdateScheduledCheckinInput(
            job_id=jid,
            about_what="should not apply",
        ),
    )
    assert result.action == "noop"


async def test_noop_on_other_bot_row(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    now = datetime.now(UTC)
    jid = _seed_checkin(
        fake_pool, user_id=user.id, bot_id="mediator", topic_id=tid,
        scheduled_for=now + timedelta(hours=1), about_what="mediator-checkin",
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await write_tools.update_scheduled_checkin(
        ctx,
        UpdateScheduledCheckinInput(
            job_id=jid,
            about_what="should not apply",
        ),
    )
    assert result.action == "noop"


async def test_noop_on_other_topic_row(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    tid = get_relationship_topic_id()
    other_tid = uuid4()
    now = datetime.now(UTC)
    jid = _seed_checkin(
        fake_pool, user_id=user.id, bot_id="hector", topic_id=other_tid,
        scheduled_for=now + timedelta(hours=1), about_what="other-topic",
    )
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await write_tools.update_scheduled_checkin(
        ctx,
        UpdateScheduledCheckinInput(
            job_id=jid,
            about_what="should not apply",
        ),
    )
    assert result.action == "noop"


async def test_noop_on_nonexistent_id(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    ctx = _build_ctx(fake_pool, user, bot_id="hector")
    result = await write_tools.update_scheduled_checkin(
        ctx,
        UpdateScheduledCheckinInput(
            job_id=uuid4(),
            about_what="should not apply",
        ),
    )
    assert result.action == "noop"
