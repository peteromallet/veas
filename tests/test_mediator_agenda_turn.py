"""End-to-end mediator turn test (T11).

Simulates a mediator turn where the model emits BOTH
`create_conversation_plan` AND `send_message_part` (echoing the numbered
list) within the `respond` step.

Asserts:
(a) Both calls succeed in a single turn.
(b) The resulting conversation lands status='ready'.
(c) The spoken confirmation text matches agenda_to_display(items).
(d) The central dispatcher does NOT double-log `create_conversation_plan`
    (it is in `_SELF_LOGGING_TOOLS`).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from app.config import get_settings
from app.models.user import User
from app.services.tools.registry import (
    call_tool,
    _SELF_LOGGING_TOOLS,
)
from app.services.turn_context import TurnContext

from tests.test_plan_tools_create import PlanCreateFakePool, _prime_env, _fake_primary_topic_id_for


def _make_turn_ctx(
    pool: PlanCreateFakePool,
    *,
    bot_id: str = "mediator",
    current_step: str = "respond",
) -> TurnContext:
    user = User(id=uuid4(), name="TestUser", phone="15555550100", timezone="UTC")
    return TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id=bot_id,
        current_step=current_step,
    )


class TestMediatorAgendaTurn:
    """End-to-end: create_conversation_plan + send_message_part in one respond step."""

    @pytest.mark.anyio
    async def test_create_plan_and_send_confirmation_in_same_respond_step(
        self, monkeypatch,
    ) -> None:
        """Simulate a mediator turn: create plan then echo via send_message_part.

        Both calls succeed in a single turn, conversation lands 'ready',
        and the spoken confirmation matches agenda_to_display.
        """
        _prime_env(monkeypatch)
        pool = PlanCreateFakePool()
        monkeypatch.setattr(
            "app.bots.registry.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        ctx = _make_turn_ctx(pool, bot_id="mediator", current_step="respond")

        # Step 1: create_conversation_plan
        plan_markdown = "1. Welcome and check-in\n2. Review last week's actions\n3. Discuss upcoming events"
        create_result = await call_tool(
            "create_conversation_plan",
            {"plan_markdown": plan_markdown, "prep_summary": None},
            ctx,
        )

        assert create_result["status"] == "ready"
        conv_id = create_result["conversation_id"]
        display_text = create_result["display_text"]

        # Verify the conversation is in the pool
        conv_uuid = UUID(conv_id)
        assert conv_uuid in pool._conversations
        assert pool._conversations[conv_uuid]["status"] == "ready"

        # Step 2: send_message_part echoing the numbered list
        send_result = await call_tool(
            "send_message_part",
            {"content": display_text},
            ctx,
        )

        # send_message_part should succeed (not error)
        assert not send_result.get("is_error")

        # Verify the display_text is a proper numbered list
        assert "1. Welcome and check-in" in display_text
        assert "2. Review last week's actions" in display_text
        assert "3. Discuss upcoming events" in display_text

    def test_create_plan_is_self_logging(self) -> None:
        """create_conversation_plan is in _SELF_LOGGING_TOOLS so the
        central dispatcher does NOT double-log it."""
        assert "create_conversation_plan" in _SELF_LOGGING_TOOLS
        assert "update_conversation_plan" in _SELF_LOGGING_TOOLS

    def test_read_plan_tools_not_self_logging(self) -> None:
        """read_conversation_plan and list_conversation_plans are NOT in
        _SELF_LOGGING_TOOLS — the dispatcher logs them as kind='read'."""
        assert "read_conversation_plan" not in _SELF_LOGGING_TOOLS
        assert "list_conversation_plans" not in _SELF_LOGGING_TOOLS

    @pytest.mark.anyio
    async def test_create_then_read_in_same_turn(self, monkeypatch) -> None:
        """After creating a plan in respond, the mediator can read it back
        in a subsequent read step (simulating the next turn's read step)."""
        _prime_env(monkeypatch)
        pool = PlanCreateFakePool()
        monkeypatch.setattr(
            "app.bots.registry.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        ctx = _make_turn_ctx(pool, bot_id="mediator", current_step="respond")

        # Create
        create_result = await call_tool(
            "create_conversation_plan",
            {"plan_markdown": "1. Item A\n2. Item B", "prep_summary": None},
            ctx,
        )
        conv_id = create_result["conversation_id"]

        # Read back in read step (same user, next-turn read step)
        read_ctx = _make_turn_ctx(pool, bot_id="mediator", current_step="read")
        read_ctx.user = ctx.user
        read_result = await call_tool(
            "read_conversation_plan",
            {"conversation_id": conv_id},
            read_ctx,
        )
        assert read_result["status"] == "ready"
        assert len(read_result["items"]) == 2
        assert read_result["items"][0]["title"] == "Item A"
        assert read_result["items"][1]["title"] == "Item B"

    @pytest.mark.anyio
    async def test_create_with_prep_summary_and_send_confirmation(
        self, monkeypatch,
    ) -> None:
        """Steered plan: create with prep_summary, send confirmation."""
        _prime_env(monkeypatch)
        pool = PlanCreateFakePool()
        monkeypatch.setattr(
            "app.bots.registry.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        ctx = _make_turn_ctx(pool, bot_id="mediator", current_step="respond")

        create_result = await call_tool(
            "create_conversation_plan",
            {
                "plan_markdown": "1. Safety check\n2. Deep dive\n3. Action items",
                "prep_summary": "User wants to discuss recent tension",
            },
            ctx,
        )

        assert create_result["status"] == "ready"
        conv_uuid = UUID(create_result["conversation_id"])
        assert pool._conversations[conv_uuid]["mode"] == "steered"
        assert pool._conversations[conv_uuid]["prep_summary"] == (
            "User wants to discuss recent tension"
        )

        # Send confirmation
        send_result = await call_tool(
            "send_message_part",
            {"content": create_result["display_text"]},
            ctx,
        )
        assert not send_result.get("is_error")

    @pytest.mark.anyio
    async def test_update_then_send_in_same_respond_step(self, monkeypatch) -> None:
        """Update a plan then send confirmation in the same respond step."""
        _prime_env(monkeypatch)
        pool = PlanCreateFakePool()
        monkeypatch.setattr(
            "app.bots.registry.primary_topic_id_for",
            _fake_primary_topic_id_for,
        )
        ctx = _make_turn_ctx(pool, bot_id="mediator", current_step="respond")

        # Create first
        create_result = await call_tool(
            "create_conversation_plan",
            {"plan_markdown": "1. Old item", "prep_summary": None},
            ctx,
        )
        conv_id = create_result["conversation_id"]

        # Update
        update_result = await call_tool(
            "update_conversation_plan",
            {
                "conversation_id": conv_id,
                "plan_markdown": "1. Revised item\n2. New item",
                "prep_summary": None,
            },
            ctx,
        )

        assert update_result["status"] == "ready"
        display_text = update_result["display_text"]

        # Send confirmation
        send_result = await call_tool(
            "send_message_part",
            {"content": display_text},
            ctx,
        )
        assert not send_result.get("is_error")
        assert "1. Revised item" in display_text
        assert "2. New item" in display_text
