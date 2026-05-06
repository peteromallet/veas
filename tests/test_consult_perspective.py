from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.models.user import User
from app.services import agentic
from app.services.tools import consult_perspective as consult_module
from app.services.tools.registry import (
    CONSULT_PHASE_TOOLS,
    READ_PHASE_TOOLS,
    TOOL_DISPATCH,
    WRITE_PHASE_TOOLS,
    call_tool,
)
from app.services.turn_context import TurnContext
from evals.capture import capture_tool_calls
from tests.conftest import FakePool
from tool_schemas import (
    ConsultPerspectiveInput,
    ConsultPerspectiveOutput,
    PerspectiveTemplate,
)

pytestmark = pytest.mark.anyio


USAGE = {
    "input_tokens": 100,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "output_tokens": 20,
}


class FakeMessages:
    def __init__(self, responses: list[SimpleNamespace], requests: list[dict]) -> None:
        self.responses = responses
        self.requests = requests

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected Anthropic request")
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[SimpleNamespace], requests: list[dict]) -> None:
        self.messages = FakeMessages(responses, requests)


class FakeAnthropicFactory:
    def __init__(self, responses: list[SimpleNamespace], requests: list[dict]) -> None:
        self.responses = responses
        self.requests = requests

    def __call__(self, **kwargs):
        return FakeClient(self.responses, self.requests)


def _response(content: list[dict], stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=dict(USAGE))


def _consult_json(**overrides) -> str:
    payload = {
        "is_error": False,
        "summary": "Use a softer first reflection.",
        "key_points": ["The draft may move too fast."],
        "suggested_moves": ["Reflect before interpreting."],
        "caveats": ["One-sided context."],
        "confidence": "medium",
        "template_used": "custom",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _ctx(pool: FakePool | None = None) -> TurnContext:
    pool = pool or FakePool()
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    pool.users[partner.id] = {"id": partner.id, "name": partner.name, "phone": partner.phone, "timezone": partner.timezone}
    return TurnContext(
        uuid4(),
        pool,
        user,
        partner,
        [uuid4()],
        current_step="consult",
        protected_owner_ids=[user.id, partner.id],
        hot_context_rendered="## Hot context\nFiltered context only.",
    )


def test_consult_schema_one_of_and_error_defaults() -> None:
    assert ConsultPerspectiveInput(template=PerspectiveTemplate.nvc, focus="reply?").template == PerspectiveTemplate.nvc
    assert ConsultPerspectiveInput(perspective="skeptical but fair", focus="reply?").perspective

    with pytest.raises(ValidationError):
        ConsultPerspectiveInput(template=PerspectiveTemplate.nvc, perspective="x", focus="reply?")
    with pytest.raises(ValidationError):
        ConsultPerspectiveInput(focus="reply?")
    with pytest.raises(ValidationError):
        ConsultPerspectiveInput(perspective="", focus="reply?")

    error = ConsultPerspectiveOutput(is_error=True, error="timeout", template_used="custom")
    assert error.summary is None
    assert error.confidence is None
    assert error.template_used == "custom"


async def test_consult_registry_gating(monkeypatch):
    async def fake_consult(ctx, args):
        return ConsultPerspectiveOutput(
            summary="ok",
            key_points=[],
            suggested_moves=[],
            caveats=[],
            confidence="low",
            template_used=args.template or "custom",
        )

    monkeypatch.setitem(TOOL_DISPATCH, "consult_perspective", fake_consult)
    ctx = _ctx()
    payload = {"template": "nvc", "focus": "how to answer"}

    read_result = await call_tool("consult_perspective", payload, ctx)
    ctx.current_step = "record"
    write_result = await call_tool("consult_perspective", payload, ctx)
    ctx.current_step = "consult"
    ctx.trigger_metadata["_inside_consult"] = True
    nested_result = await call_tool("consult_perspective", payload, ctx)

    assert read_result["summary"] == "ok"
    assert write_result["is_error"] is True and write_result["error"].startswith("step:")
    assert nested_result["is_error"] is True and nested_result["error"].startswith("step:")


def test_consult_allowlist_is_read_only_without_send_or_recursion() -> None:
    assert "consult_perspective" in READ_PHASE_TOOLS
    assert "consult_perspective" not in WRITE_PHASE_TOOLS
    assert CONSULT_PHASE_TOOLS == READ_PHASE_TOOLS - {"send_message_part", "consult_perspective"}
    assert "send_message_part" not in CONSULT_PHASE_TOOLS
    assert "consult_perspective" not in CONSULT_PHASE_TOOLS
    assert not (CONSULT_PHASE_TOOLS & WRITE_PHASE_TOOLS)


def test_all_templates_have_nonempty_bodies() -> None:
    assert set(consult_module.PERSPECTIVE_TEMPLATES) == set(PerspectiveTemplate)
    assert all(body.strip() for body in consult_module.PERSPECTIVE_TEMPLATES.values())


async def test_consult_uses_configured_model_and_safe_tools(app_env, monkeypatch):
    monkeypatch.setenv("CONSULT_MODEL", "claude-consult-test")
    get_settings.cache_clear()
    requests: list[dict] = []
    monkeypatch.setattr(
        consult_module.anthropic,
        "AsyncAnthropic",
        FakeAnthropicFactory(
            [_response([{"type": "text", "text": _consult_json()}])],
            requests,
        ),
    )
    ctx = _ctx()

    result = await consult_module.consult_perspective(
        ctx,
        ConsultPerspectiveInput(template=PerspectiveTemplate.gottman, focus="critique the reply"),
    )

    assert result.summary == "Use a softer first reflection."
    assert result.template_used == PerspectiveTemplate.gottman
    assert requests[0]["model"] == "claude-consult-test"
    assert requests[0]["max_tokens"] == 600
    tool_names = {tool["name"] for tool in requests[0]["tools"]}
    assert tool_names == CONSULT_PHASE_TOOLS


@pytest.mark.parametrize(
    ("side_effect", "needle"),
    [
        (asyncio.TimeoutError(), "timed out"),
        (agentic.SpendCapExceeded("cap hit"), "cap hit"),
        (agentic.BoundedLoopExceeded("too many"), "too many"),
        (agentic.LLMPhaseError("llm failed"), "consult failed: llm failed"),
    ],
)
async def test_consult_graceful_loop_errors(app_env, monkeypatch, side_effect, needle):
    async def fake_run_step(*args, **kwargs):
        raise side_effect

    monkeypatch.setattr(consult_module, "run_step", fake_run_step)
    result = await consult_module.consult_perspective(
        _ctx(),
        ConsultPerspectiveInput(perspective="custom", focus="reply"),
    )

    assert result.is_error is True
    assert needle in result.error
    assert result.template_used == "custom"


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("not json", "invalid consult JSON"),
        (json.dumps({"is_error": False, "summary": "missing confidence"}), "invalid consult output"),
    ],
)
async def test_consult_graceful_output_errors(app_env, monkeypatch, text, needle):
    requests: list[dict] = []
    monkeypatch.setattr(
        consult_module.anthropic,
        "AsyncAnthropic",
        FakeAnthropicFactory([_response([{"type": "text", "text": text}])], requests),
    )

    result = await consult_module.consult_perspective(
        _ctx(),
        ConsultPerspectiveInput(perspective="custom", focus="reply"),
    )

    assert result.is_error is True
    assert needle in result.error


async def test_consult_inner_read_calls_are_captured_with_consult_phase(app_env, monkeypatch):
    requests: list[dict] = []
    ctx = _ctx()
    memory_id = uuid4()
    ctx.pool.memories[memory_id] = {
        "id": memory_id,
        "about_user_id": ctx.user.id,
        "content": "Maya likes direct repair.",
        "status": "active",
        "related_theme_ids": [],
        "created_at": datetime.now(UTC),
        "last_referenced_at": None,
    }
    monkeypatch.setattr(
        consult_module.anthropic,
        "AsyncAnthropic",
        FakeAnthropicFactory(
            [
                _response(
                    [{"type": "tool_use", "id": "toolu_inner", "name": "get_memories", "input": {"about_user_id": str(ctx.user.id)}}],
                    "tool_use",
                ),
                _response([{"type": "text", "text": _consult_json()}]),
            ],
            requests,
        ),
    )

    with capture_tool_calls() as transcript:
        result = await call_tool(
            "consult_perspective",
            {"template": "reflective_listener", "focus": "how to answer"},
            ctx,
        )

    assert result["summary"] == "Use a softer first reflection."
    assert [(call.tool_name, call.phase) for call in transcript.calls] == [
        ("get_memories", "consult"),
        ("consult_perspective", "consult"),
    ]


async def test_consult_search_messages_inherits_privacy_scope(app_env, monkeypatch):
    requests: list[dict] = []
    pool = FakePool()
    ctx = _ctx(pool)
    message_id = uuid4()
    pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": ctx.partner.id,
        "recipient_id": None,
        "content": "private partner detail",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "deleted_at": None,
        "media_type": None,
        "media_url": None,
        "media_analysis": None,
    }
    monkeypatch.setattr(
        consult_module.anthropic,
        "AsyncAnthropic",
        FakeAnthropicFactory(
            [
                _response(
                    [{"type": "tool_use", "id": "toolu_search", "name": "search_messages", "input": {"limit": 5}}],
                    "tool_use",
                ),
                _response([{"type": "text", "text": _consult_json()}]),
            ],
            requests,
        ),
    )

    with capture_tool_calls() as transcript:
        await call_tool("consult_perspective", {"template": "nvc", "focus": "reply"}, ctx)

    search_result = transcript.calls[0].result
    assert search_result["hits"] == []


async def test_main_run_step_round_trips_consult_tool_result(app_env, monkeypatch):
    consult_requests: list[dict] = []
    main_requests: list[dict] = []
    ctx = _ctx()
    monkeypatch.setattr(
        consult_module.anthropic,
        "AsyncAnthropic",
        FakeAnthropicFactory([_response([{"type": "text", "text": _consult_json()}])], consult_requests),
    )
    main_client = FakeClient(
        [
            _response(
                [{"type": "tool_use", "id": "toolu_consult", "name": "consult_perspective", "input": {"template": "nvc", "focus": "reply"}}],
                "tool_use",
            ),
            _response([{"type": "text", "text": "final reply"}]),
        ],
        main_requests,
    )

    assistant_text, messages, tool_count = await agentic.run_step(
        main_client,
        ctx,
        "system",
        ctx.hot_context_rendered or "",
        READ_PHASE_TOOLS,
        [{"role": "user", "content": "Phase A"}],
    )

    tool_result = messages[2]["content"][0]
    payload = json.loads(tool_result["content"])
    assert assistant_text == "final reply"
    assert tool_count == 1
    assert tool_result["type"] == "tool_result"
    assert payload["summary"] == "Use a softer first reflection."
    assert payload["template_used"] == "nvc"
