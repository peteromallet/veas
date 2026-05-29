"""Tests for live voice turn context."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.bots.base import BotSpec
from app.bots.registry import BOT_SPECS
from app.services.live.schemas import TurnEmission, TurnRequest
from app.services.live.turn_loop import (
    DeepseekTurnCaller,
    FallbackTurnCaller,
    _trim_rendered_hot_context,
    load_turn_context,
    select_turn_caller,
)


class _TurnFakePool:
    def __init__(self, conversation: dict[str, Any], user: dict[str, Any]) -> None:
        self.conversation = conversation
        self.user = user

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "FROM mediator.conversations" in sql:
            return self.conversation
        if "FROM users" in sql:
            return self.user
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return []


@pytest.mark.anyio
async def test_load_turn_context_includes_selected_bot_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: live turns must not default to mediator instructions."""

    def prompt_renderer(
        assistant_name: str,
        user_name: str,
        partner_name: str | None = None,
        **_: Any,
    ) -> str:
        del partner_name
        return (
            f"{assistant_name} pregnancy prompt for {user_name}; "
            "speak German when apt."
        )

    monkeypatch.setitem(
        BOT_SPECS,
        "rosi_live_test",
        BotSpec(
            bot_id="rosi_live_test",
            prompt_renderer=prompt_renderer,
            step_instructions={
                "read": "read",
                "consult": "consult",
                "respond": "respond",
                "record": "record",
                "schedule": "schedule",
                "done": "done",
            },
            display_name="Tante Rosi",
            primary_topic_slug="pregnancy",
            participants_shape="solo",
        ),
    )

    session_id = uuid4()
    user_id = uuid4()
    pool = _TurnFakePool(
        conversation={
            "id": session_id,
            "user_id": user_id,
            "bot_id": "rosi_live_test",
            "prep_summary": "prep",
            "current_item_id": None,
            "session_fields": {},
            "status": "active",
        },
        user={
            "id": user_id,
            "name": "Maya",
            "phone": "+15555550100",
            "timezone": "Europe/Berlin",
            "onboarding_state": "ready",
            "pacing_preferences": {},
        },
    )

    context = await load_turn_context(pool, session_id)

    profile = context["bot_profile"]
    assert profile["bot_id"] == "rosi_live_test"
    assert profile["display_name"] == "Tante Rosi"
    assert profile["primary_topic_slug"] == "pregnancy"
    assert "pregnancy prompt for Maya" in profile["system_prompt"]
    assert context["temporal_anchor"]["timezone"] == "Europe/Berlin"
    assert context["temporal_anchor"]["local_day"]


@pytest.mark.anyio
async def test_load_turn_context_includes_selected_bot_hot_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live turns should receive the same rendered context as chat turns."""

    def prompt_renderer(
        assistant_name: str,
        user_name: str,
        partner_name: str | None = None,
        **_: Any,
    ) -> str:
        del assistant_name, user_name, partner_name
        return "hector prompt"

    monkeypatch.setitem(
        BOT_SPECS,
        "hector_live_test",
        BotSpec(
            bot_id="hector_live_test",
            prompt_renderer=prompt_renderer,
            step_instructions={
                "read": "read",
                "consult": "consult",
                "respond": "respond",
                "record": "record",
                "schedule": "schedule",
                "done": "done",
            },
            display_name="Hector",
            primary_topic_slug="fitness",
            participants_shape="solo",
        ),
    )

    calls = 0

    async def fake_build_hot_context_solo(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        assert kwargs["bot_id"] == "hector_live_test"
        return {"ok": True}

    def fake_render_hot_context_solo(hot_context: dict[str, Any]) -> str:
        assert hot_context == {"ok": True}
        return "## Fitness\nTuesday: missed\nWednesday: missed\nThursday: blank"

    monkeypatch.setattr(
        "app.services.hot_context_solo.build_hot_context_solo",
        fake_build_hot_context_solo,
    )
    monkeypatch.setattr(
        "app.services.hot_context_solo.render_hot_context_solo",
        fake_render_hot_context_solo,
    )

    session_id = uuid4()
    user_id = uuid4()
    topic_id = uuid4()
    pool = _TurnFakePool(
        conversation={
            "id": session_id,
            "user_id": user_id,
            "partner_user_id": None,
            "bot_id": "hector_live_test",
            "prep_summary": "prep",
            "current_item_id": None,
            "session_fields": {},
            "status": "active",
            "topic_id": topic_id,
        },
        user={
            "id": user_id,
            "name": "Peter",
            "phone": "+15555550100",
            "timezone": "Europe/Berlin",
            "onboarding_state": "ready",
            "pacing_preferences": {},
        },
    )

    context = await load_turn_context(pool, session_id)
    context_again = await load_turn_context(pool, session_id)

    assert context["hot_context_rendered"] == (
        "## Fitness\nTuesday: missed\nWednesday: missed\nThursday: blank"
    )
    assert context_again["hot_context_rendered"] == context["hot_context_rendered"]
    assert calls == 1


def test_trim_rendered_hot_context_keeps_live_grounding_sections() -> None:
    rendered = "\n\n".join(
        [
            "## You\n- name: Peter",
            "## Fitness\nTuesday: missed\nWednesday: missed\nThursday: blank",
            "## Distillations\n" + ("large irrelevant block\n" * 1000),
            "## Recent messages\n- user: Are we talking about last week?",
        ]
    )

    trimmed = _trim_rendered_hot_context(rendered)

    assert "## Fitness" in trimmed
    assert "Thursday: blank" in trimmed
    assert "## Recent messages" in trimmed
    assert "## Distillations" not in trimmed
    assert len(trimmed) < len(rendered)


def test_select_turn_caller_wraps_anthropic_with_deepseek_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prod regression: a billing-blocked Anthropic key must not silence live turns."""

    monkeypatch.setenv("LIVE_VOICE_TURN_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-looking")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-real-looking")

    caller = select_turn_caller()

    assert isinstance(caller, FallbackTurnCaller)
    assert caller.primary_name == "anthropic"
    assert caller.fallback_name == "deepseek"


def test_select_turn_caller_uses_settings_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local .env-backed settings must not fall through to the stub caller."""

    monkeypatch.delenv("LIVE_VOICE_TURN_PROVIDER", raising=False)
    monkeypatch.setenv("LIVE_VOICE_TURN_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-real-looking")
    from app.config import get_settings

    get_settings.cache_clear()

    caller = select_turn_caller()

    assert isinstance(caller, DeepseekTurnCaller)


@pytest.mark.anyio
async def test_fallback_turn_caller_uses_secondary_after_primary_failure() -> None:
    class Primary:
        async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission:
            raise RuntimeError("credit balance too low")

    class Secondary:
        async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission:
            return TurnEmission(utterance=f"reply to {request.user_transcript_final}")

    caller = FallbackTurnCaller(
        Primary(),
        Secondary(),
        primary_name="anthropic",
        fallback_name="deepseek",
    )

    emission = await caller.call(
        TurnRequest(session_id=str(uuid4()), user_transcript_final="Hey, can you hear me?"),
        {},
    )

    assert emission.utterance == "reply to Hey, can you hear me?"
