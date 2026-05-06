from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.user import User
from app.services.turn_context import TurnContext
from app.services.turn_plan import SKELETONS, make_turn_plan, orient_summary, pick_default_skeleton
from app.services.tools.registry import STEP_ALLOWED_TOOLS, call_tool
from tests.conftest import FakePool

pytestmark = pytest.mark.anyio


def test_pick_default_skeleton_covers_named_paths() -> None:
    assert pick_default_skeleton(trigger_metadata={"messages": [{"content": "hi"}]}, charge=None) == "quick_reply"
    assert pick_default_skeleton(trigger_metadata={"messages": [{"content": "ok"}]}, charge=None) == "silence_or_react"
    assert pick_default_skeleton(trigger_metadata={"messages": [{"content": "remember my new job"}]}, charge=None) == "standard"
    assert pick_default_skeleton(trigger_metadata={"messages": [{"content": "why did you send that"}]}, charge=None) == "standard"
    assert pick_default_skeleton(trigger_metadata={"kind": "scheduled_task"}, charge=None) == "standard"
    assert pick_default_skeleton(trigger_metadata={"messages": [{"content": "this hurt"}]}, charge="charged") == "charged"
    assert pick_default_skeleton(trigger_metadata={"messages": [{"content": "I might hurt myself"}]}, charge="crisis") == "crisis"
    assert SKELETONS["quick_reply"] == ["respond", "done"]


def test_orient_summary_is_nonempty_runner_context() -> None:
    text = orient_summary(
        trigger_metadata={"kind": "inbound", "context": {"source": "test"}},
        charge="routine",
        hot_context_signals={"recent_message_count": 2},
    )

    assert text.startswith("Orient: ")
    assert "recent_message_count" in text


async def test_update_turn_plan_mutates_without_tool_capture(fake_pool: FakePool) -> None:
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    plan = make_turn_plan("quick_reply")
    ctx = TurnContext(uuid4(), fake_pool, user, partner, [uuid4()], current_step="respond", turn_plan=plan)

    result = await call_tool(
        "update_turn_plan",
        {"add_steps": ["record"], "note": "needs durable state"},
        ctx,
    )

    assert result["current"] == "respond"
    assert result["steps"] == ["respond", "record", "done"]
    assert result["notes"] == ["needs durable state"]
    assert fake_pool.tool_calls == []


async def test_update_turn_plan_inserts_before_later_steps_and_keeps_current(fake_pool: FakePool) -> None:
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    plan = make_turn_plan("standard")
    ctx = TurnContext(uuid4(), fake_pool, user, partner, [uuid4()], current_step="read", turn_plan=plan)

    add_result = await call_tool("update_turn_plan", {"add_steps": ["consult", "consult"]}, ctx)

    assert add_result["steps"] == ["read", "consult", "respond", "record", "schedule", "done"]
    remove_result = await call_tool("update_turn_plan", {"remove_steps": ["read", "schedule"]}, ctx)
    assert remove_result["current"] == "read"
    assert remove_result["steps"] == ["read", "consult", "respond", "record", "done"]


async def test_record_step_rejects_user_facing_tools(fake_pool: FakePool) -> None:
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    ctx = TurnContext(uuid4(), fake_pool, user, partner, [uuid4()], current_step="record")

    send_result = await call_tool("send_message_part", {"content": "hello"}, ctx)
    consult_result = await call_tool("consult_perspective", {"template": "nvc", "focus": "reply"}, ctx)

    assert send_result["is_error"] is True
    assert consult_result["is_error"] is True
    assert "send_message_part" not in STEP_ALLOWED_TOOLS["record"]
    assert "consult_perspective" not in STEP_ALLOWED_TOOLS["record"]
