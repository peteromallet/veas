from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.user import User
from app.services import agentic
from app.services.deepseek import _to_anthropic_like_response, _to_openai_messages
from app.services.agentic import BoundedLoopExceeded, run_step
from app.services.turn_context import TurnContext
from app.services.tools.registry import READ_PHASE_TOOLS, STEP_ALLOWED_TOOLS, WRITE_PHASE_TOOLS
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


class FailingMessages:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def create(self, **kwargs):
        self.events.append("deepseek_call")
        raise RuntimeError("deepseek 400")


class FailingClient:
    def __init__(self, events: list[str]) -> None:
        self.messages = FailingMessages(events)


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
    return TurnContext(uuid4(), pool, user, partner, [uuid4()], current_step="read")


async def test_run_step_uses_tools_cache_markers_and_records_spend(app_env):
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

    assistant_text, messages, tool_count = await run_step(
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
        "client_call",
        "record_cost",
        "cap_check",
        "client_call",
        "record_cost",
        "cap_check",
    ]
    assert pool.llm_spend_log["text"] == Decimal("0.004815")


async def test_read_step_tool_cap_advances_without_failing_turn(app_env):
    events: list[str] = []
    requests: list[dict] = []
    pool = FakePool()
    ctx = _ctx(pool)
    responses = [
        _response(
            [{"type": "tool_use", "id": "toolu_1", "name": "get_memories", "input": {}}],
            _usage(100, 0, 0, 20),
            "tool_use",
        ),
        _response(
            [{"type": "tool_use", "id": "toolu_2", "name": "get_observations", "input": {}}],
            _usage(100, 0, 0, 20),
            "tool_use",
        ),
    ]
    client = FakeClient(responses, requests, events)

    assistant_text, _messages, tool_count = await run_step(
        client,
        ctx,
        "system prompt",
        "context",
        READ_PHASE_TOOLS,
        [{"role": "user", "content": "Phase A"}],
        max_tool_iterations=1,
    )

    assert assistant_text == ""
    assert tool_count == 1
    assert len(requests) == 2


async def test_run_step_applies_cache_markers_in_record_step(app_env):
    events: list[str] = []
    requests: list[dict] = []
    pool = TrackingPool(events)
    ctx = _ctx(pool)
    ctx.current_step = "record"
    client = FakeClient(
        [_response([{"type": "text", "text": "write note"}], _usage(100, 0, 0, 10))],
        requests,
        events,
    )

    assistant_text, _, tool_count = await run_step(
        client,
        ctx,
        "system prompt",
        "small context",
        STEP_ALLOWED_TOOLS["record"],
        [{"role": "user", "content": "Record"}],
    )

    assert assistant_text == "write note"
    assert tool_count == 0
    assert requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in requests[0]["system"][1]
    assert requests[0]["tools"][-1]["cache_control"] == {"type": "ephemeral"}


async def test_run_step_records_spend_without_blocking_on_caps(app_env):
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

    assistant_text, _, tool_count = await run_step(
        client,
        ctx,
        "system",
        "context",
        READ_PHASE_TOOLS,
        [{"role": "user", "content": "Phase A"}],
    )

    assert assistant_text == "should not run"
    assert tool_count == 0
    assert events == ["client_call", "record_cost", "cap_check"]
    assert len(requests) == 1


async def test_run_step_can_record_deepseek_priced_usage(app_env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_INPUT_USD_PER_MTOK", "0.27")
    monkeypatch.setenv("DEEPSEEK_OUTPUT_USD_PER_MTOK", "1.10")
    from app.config import get_settings

    get_settings.cache_clear()
    events: list[str] = []
    requests: list[dict] = []
    pool = TrackingPool(events)
    ctx = _ctx(pool)
    client = FakeClient(
        [
            _response(
                [{"type": "text", "text": "deepseek reply"}],
                _usage(1000, 0, 0, 100),
            )
        ],
        requests,
        events,
    )

    assistant_text, _, tool_count = await run_step(
        client,
        ctx,
        "system",
        "context",
        READ_PHASE_TOOLS,
        [{"role": "user", "content": "Phase A"}],
        model="deepseek-chat",
        provider="deepseek",
    )

    assert assistant_text == "deepseek reply"
    assert tool_count == 0
    assert pool.llm_spend_log["text"] == Decimal("0.000380")
    get_settings.cache_clear()


async def test_deepseek_failure_falls_back_to_anthropic(app_env, monkeypatch):
    """When a provider_chain=('deepseek','anthropic') is passed and DeepSeek
    raises a transient error, the chain advances to Anthropic after one retry.

    With Project A2 the fallback behaviour is opt-in via the explicit
    ``provider_chain`` kwarg; the legacy hard-coded ``provider="deepseek"``
    path no longer cascades automatically (length-1 chain).
    """
    events: list[str] = []
    requests: list[dict] = []
    pool = TrackingPool(events)
    ctx = _ctx(pool)
    fallback = FakeClient(
        [_response([{"type": "text", "text": "fallback reply"}], _usage(100, 0, 0, 20))],
        requests,
        events,
    )
    monkeypatch.setattr(agentic.anthropic, "AsyncAnthropic", lambda api_key: fallback)
    monkeypatch.setattr(
        agentic, "DeepSeekClient", lambda: FailingClient(events)
    )
    # Reset the per-bot fallback breaker so unrelated tests don't pre-trip it.
    agentic._FALLBACK_BREAKER.reset()

    assistant_text, _, tool_count = await run_step(
        None,
        ctx,
        "system",
        "context",
        READ_PHASE_TOOLS,
        [
            {"role": "user", "content": "Phase A"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "openai_assistant_message",
                        "message": {"role": "assistant", "content": "native"},
                    },
                    {"type": "text", "text": "keep me"},
                ],
            },
        ],
        provider_chain=("deepseek", "anthropic"),
    )

    assert assistant_text == "fallback reply"
    assert tool_count == 0
    assert events.count("deepseek_call") == 1
    assert events.count("client_call") == 1
    assert requests[0]["messages"][1]["content"] == [
        {"type": "text", "text": "keep me"}
    ]


def test_all_users_route_to_deepseek(app_env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_CONVERSATIONAL_MODEL", "deepseek-chat")
    from app.config import get_settings

    get_settings.cache_clear()
    for name in ("Peter", "Hannah"):
        user = User(uuid4(), name, "15555550100", "UTC")
        _, model, provider = agentic._llm_client_and_model_for_user(user)
        assert (model, provider) == ("deepseek-chat", "deepseek")
    get_settings.cache_clear()


def test_deepseek_adapter_round_trips_tool_calls():
    response = _to_anthropic_like_response(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "Checking.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_memories",
                                    "arguments": '{"about_user_id":"u1"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )
    blocks = [agentic._block_to_dict(block) for block in response.content]

    assert response.stop_reason == "tool_use"
    assert blocks[-1] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "get_memories",
        "input": {"about_user_id": "u1"},
    }

    messages = _to_openai_messages(
        [{"type": "text", "text": "system"}],
        [
            {"role": "assistant", "content": blocks},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": '{"ok": true}',
                    }
                ],
            },
        ],
    )

    assert messages[1]["tool_calls"][0]["function"]["name"] == "get_memories"
    assert messages[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"ok": true}',
    }


# ── helpers for Hector tool-error-recovery tests ──────────────────────────


def _hector_ctx(pool: FakePool) -> TurnContext:
    """TurnContext with bot_id='hector', fitness topic, and 'record' step."""
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    pool.users[partner.id] = {"id": partner.id, "name": partner.name, "phone": partner.phone, "timezone": partner.timezone}
    fitness_topic_id = uuid4()
    return TurnContext(
        uuid4(), pool, user, partner, [uuid4()],
        bot_id="hector",
        primary_topic_id=fitness_topic_id,
        primary_topic_slug="fitness",
        current_step="record",
    )


# ── T10: same-turn correction integration tests ──────────────────────────


async def test_run_step_corrects_after_recoverable_log_event_error(app_env):
    """Model calls log_event(pending) → error → loop continues → model corrects → turn completes.

    Assertions:
    - First response calls log_event with 'pending', tool returns is_error=true with correction_hint.
    - Loop continues (no crash).
    - Second response produces corrected behavior (no more placeholder IDs).
    - Turn completes with assistant text and correct tool count.
    """
    events: list[str] = []
    requests: list[dict] = []
    pool = FakePool()
    ctx = _hector_ctx(pool)

    responses = [
        # Iteration 1: log_event with placeholder ID → recoverable error
        _response(
            [{"type": "tool_use", "id": "toolu_1", "name": "log_event", "input": {
                "commitment_id": "pending",
                "metric_key": "workout_session",
                "adherence_status": "done",
            }}],
            _usage(100, 0, 0, 50),
            "tool_use",
        ),
        # Iteration 2: model produces text (corrected — no more placeholder IDs)
        _response(
            [{"type": "text", "text": "Let me look up your commitments first before logging."}],
            _usage(100, 0, 0, 20),
            "end_turn",
        ),
    ]
    client = FakeClient(responses, requests, events)

    assistant_text, messages, tool_count = await run_step(
        client,
        ctx,
        "system prompt",
        "context",
        STEP_ALLOWED_TOOLS["record"],
        [{"role": "user", "content": "Log my workout"}],
    )

    # Turn completed with corrected behavior
    assert assistant_text == "Let me look up your commitments first before logging."
    assert tool_count == 1  # Only log_event counted (update_turn_plan is excluded)
    assert len(requests) == 2  # Two model calls

    # Verify the error result was model-visible
    # The tool_result message should contain is_error metadata
    tool_result_msg = messages[2]  # After [seed, assistant, tool_result]
    assert tool_result_msg["role"] == "user"
    tool_result_blocks = tool_result_msg["content"]
    assert len(tool_result_blocks) == 1
    assert tool_result_blocks[0]["is_error"] is True

    # Parse the JSON content to verify structured error fields
    import json
    payload = json.loads(tool_result_blocks[0]["content"])
    assert payload.get("error_code") == "invalid_uuid"
    assert payload.get("field") == "commitment_id"
    assert payload.get("retryable") is True
    assert "correction_hint" in payload
    assert "list_commitments" in payload["correction_hint"]


async def test_run_step_repeated_validation_errors_exhaust_cap(app_env):
    """Three consecutive recoverable validation errors → BoundedLoopExceeded.

    After 2 consecutive iterations with retryable=True errors, the cap fires
    with failure_reason='tool_validation_recoverable_exhausted'.  The third
    model response is never consumed.

    Assertions:
    - First response: log_event(pending) → error (retryable).
    - Second response: log_event(unknown) → error (retryable).
    - Cap fires: BoundedLoopExceeded with 'tool_validation_recoverable_exhausted'.
    - Only 2 model requests were made (third response never consumed).
    - Error is a structured BoundedLoopExceeded, NOT a raw asyncpg.DataError.
    """
    events: list[str] = []
    requests: list[dict] = []
    pool = FakePool()
    ctx = _hector_ctx(pool)

    responses = [
        # Iteration 1: log_event(pending) → recoverable error
        _response(
            [{"type": "tool_use", "id": "toolu_1", "name": "log_event", "input": {
                "commitment_id": "pending",
                "metric_key": "workout_session",
                "adherence_status": "done",
            }}],
            _usage(100, 0, 0, 50),
            "tool_use",
        ),
        # Iteration 2: log_event(unknown) → recoverable error → cap fires
        _response(
            [{"type": "tool_use", "id": "toolu_2", "name": "log_event", "input": {
                "commitment_id": "unknown",
                "metric_key": "workout_session",
                "adherence_status": "done",
            }}],
            _usage(100, 0, 0, 50),
            "tool_use",
        ),
        # Iteration 3 (never reached): log_event(todo)
        _response(
            [{"type": "tool_use", "id": "toolu_3", "name": "log_event", "input": {
                "commitment_id": "todo",
                "metric_key": "workout_session",
                "adherence_status": "done",
            }}],
            _usage(100, 0, 0, 50),
            "tool_use",
        ),
    ]
    client = FakeClient(responses, requests, events)

    with pytest.raises(BoundedLoopExceeded) as exc_info:
        await run_step(
            client,
            ctx,
            "system prompt",
            "context",
            STEP_ALLOWED_TOOLS["record"],
            [{"role": "user", "content": "Log my workout"}],
        )

    # Verify structured failure — NOT a raw asyncpg.DataError
    assert exc_info.value.failure_reason == "tool_validation_recoverable_exhausted"
    assert len(requests) == 2  # Only 2 model calls; third response never consumed
