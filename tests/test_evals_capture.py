from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.user import User
from app.services.turn_context import TurnContext
from app.services.tools.registry import call_tool
from evals.capture import capture_tool_calls
from tests.conftest import FakePool

pytestmark = pytest.mark.anyio


def _ctx(pool: FakePool, *, phase: str) -> TurnContext:
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    pool.users[partner.id] = {"id": partner.id, "name": partner.name, "phone": partner.phone, "timezone": partner.timezone}
    return TurnContext(uuid4(), pool, user, partner, [uuid4()], phase=phase)


async def test_capture_records_read_and_write_once_without_duplicate_persisted_rows(fake_pool: FakePool) -> None:
    read_ctx = _ctx(fake_pool, phase="read")
    observation_id = uuid4()
    fake_pool.observations[observation_id] = {
        "id": observation_id,
        "about_user_id": read_ctx.user.id,
        "content": "Maya notices repair attempts land better in person.",
        "confidence": "medium",
        "significance": 3,
        "status": "active",
        "related_theme_ids": [],
        "supporting_message_ids": [],
        "created_at": datetime.now(UTC),
        "last_reinforced_at": None,
        "surfaced_count": 0,
    }

    with capture_tool_calls() as transcript:
        read_result = await call_tool(
            "get_observations",
            {"about_user_id": str(read_ctx.user.id), "min_significance": 3},
            read_ctx,
        )
        write_ctx = TurnContext(
            read_ctx.turn_id,
            fake_pool,
            read_ctx.user,
            read_ctx.partner,
            read_ctx.triggering_message_ids,
            phase="write",
        )
        write_result = await call_tool(
            "update_observation",
            {"observation_id": str(observation_id), "content": "Updated repair observation."},
            write_ctx,
        )

    assert "observations" in read_result
    assert write_result["id"] == str(observation_id)
    assert [call.tool_name for call in transcript.calls] == ["get_observations", "update_observation"]
    assert [call.phase for call in transcript.calls] == ["read", "write"]
    assert len(fake_pool.tool_calls) == 1
    assert fake_pool.tool_calls[0]["tool_name"] == "update_observation"


async def test_capture_records_validation_errors_without_persisting(fake_pool: FakePool) -> None:
    ctx = _ctx(fake_pool, phase="read")

    with capture_tool_calls() as transcript:
        result = await call_tool("get_observations", {"min_significance": 999}, ctx)

    assert result["is_error"] is True
    assert transcript.calls[0].tool_name == "get_observations"
    assert transcript.calls[0].result["is_error"] is True
    assert fake_pool.tool_calls == []
