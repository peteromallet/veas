from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.user import User
from app.services.agentic import SpendCapExceeded, run_phase
from app.services.turn_context import TurnContext
from app.services.tools.registry import READ_PHASE_TOOLS, WRITE_PHASE_TOOLS
from tests.conftest import FakePool

pytestmark = pytest.mark.anyio


class TrackingPool(FakePool):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    async def fetchval(self, sql: str, *args):
        compact = " ".join(sql.split())
        if "SELECT total_usd" in compact and "FROM llm_spend_log" in compact:
            self.events.append("cap_check")
        return await super().fetchval(sql, *args)

    async def execute(self, sql: str, *args) -> str:
        if "INSERT INTO llm_spend_log" in " ".join(sql.split()):
            self.events.append("record_cost")
        return await super().execute(sql, *args)


class FakeMessages:
    def __init__(self, responses: list[SimpleNamespace], requests: list[dict], events: list[str]) -> None:
        self.responses = responses
        self.requests = requests
        self.events = events

    async def create(self, **kwargs):
        self.events.append("client_call")
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected Anthropic call")
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[SimpleNamespace], requests: list[dict], events: list[str]) -> None:
        self.messages = FakeMessages(responses, requests, events)


def _usage(input_tokens: int, cache_create: int, cache_read: int, output_tokens: int) -> dict:
    return {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_create,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output_tokens,
    }


def _response(content: list[dict], usage: dict, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(content=content, usage=usage, stop_reason=stop_reason)


def _ctx(pool: FakePool) -> TurnContext:
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    pool.users[partner.id] = {"id": partner.id, "name": partner.name, "phone": partner.phone, "timezone": partner.timezone}
    return TurnContext(uuid4(), pool, user, partner, [uuid4()], phase="read")


async def test_run_phase_uses_tools_cache_markers_and_records_spend(app_env):
    events: list[str] = []
    requests: list[dict] = []
    pool = TrackingPool(events)
    ctx = _ctx(pool)
    memory_id = uuid4()
    pool.memories[memory_id] = {
        "id": memory_id,
        "about_user_id": ctx.user.id,
        "content": "Maya prefers direct wording.",
        "status": "active",
        "related_theme_ids": [],
        "created_at": datetime.now(UTC),
        "last_referenced_at": None,
    }
    responses = [
        _response(
            [{"type": "tool_use", "id": "toolu_1", "name": "get_memories", "input": {"about_user_id": str(ctx.user.id)}}],
            _usage(1000, 100, 200, 50),
            "tool_use",
        ),
        _response([{"type": "text", "text": "I hear you."}], _usage(500, 0, 100, 20)),
    ]
    client = FakeClient(responses, requests, events)
    hot_context = "x" * 4096

    assistant_text, messages, tool_count = await run_phase(
        client,
        ctx,
        "system prompt",
        hot_context,
        READ_PHASE_TOOLS,
        [{"role": "user", "content": "Phase A"}],
    )

    assert assistant_text == "I hear you."
    assert tool_count == 1
    assert len(requests) == 2
    assert requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert requests[0]["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert requests[0]["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert requests[1]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert requests[1]["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert messages[2]["content"][0]["type"] == "tool_result"
    assert events == [
        "cap_check",
        "client_call",
        "record_cost",
        "cap_check",
        "cap_check",
        "client_call",
        "record_cost",
        "cap_check",
    ]
    assert pool.llm_spend_log["text"] == Decimal("0.004815")


async def test_run_phase_applies_cache_markers_in_write_phase(app_env):
    events: list[str] = []
    requests: list[dict] = []
    pool = TrackingPool(events)
    ctx = _ctx(pool)
    ctx.phase = "write"
    client = FakeClient(
        [_response([{"type": "text", "text": "write note"}], _usage(100, 0, 0, 10))],
        requests,
        events,
    )

    assistant_text, _, tool_count = await run_phase(
        client,
        ctx,
        "system prompt",
        "small context",
        WRITE_PHASE_TOOLS,
        [{"role": "user", "content": "Phase B"}],
    )

    assert assistant_text == "write note"
    assert tool_count == 0
    assert requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in requests[0]["system"][1]
    assert requests[0]["tools"][-1]["cache_control"] == {"type": "ephemeral"}


async def test_run_phase_checks_spend_cap_before_client_call(app_env):
    events: list[str] = []
    requests: list[dict] = []
    pool = TrackingPool(events)
    pool.llm_spend_log["text"] = Decimal("10")
    ctx = _ctx(pool)
    client = FakeClient(
        [_response([{"type": "text", "text": "should not run"}], _usage(1, 0, 0, 1))],
        requests,
        events,
    )

    with pytest.raises(SpendCapExceeded):
        await run_phase(client, ctx, "system", "context", READ_PHASE_TOOLS, [{"role": "user", "content": "Phase A"}])

    assert events == ["cap_check"]
    assert requests == []
