from __future__ import annotations

from uuid import uuid4

from app.bots.coach import build_coach_spec
from app.bots.hector import build_hector_spec
from app.bots.habits import build_habits_spec
from app.bots.mediator import MEDIATOR_BOT
from app.bots.tante_rosi import build_tante_rosi_spec
from app.models.user import User
from app.services.tools.registry import (
    BOT_EXCLUSIVE_TOOLS,
    READ_PHASE_TOOLS,
    TOOL_DESCRIPTIONS,
    TOOL_DISPATCH,
    _step_allowed,
)
from app.services.turn_context import TurnContext


NEW_NAV_AND_SEARCH_TOOLS = frozenset({
    "messages_before",
    "messages_after",
    "open_thread",
    "scroll",
    "topic_recent",
    "search",
})


def _specs():
    return {
        "coach": build_coach_spec(),
        "hector": build_hector_spec(),
        "habits": build_habits_spec(),
        "tante_rosi": build_tante_rosi_spec(),
        "mediator": MEDIATOR_BOT,
    }


def _step_allowed_for(bot_id: str) -> set[str]:
    spec = _specs()[bot_id]
    ctx = TurnContext(
        turn_id=uuid4(),
        pool=None,
        user=User(id=uuid4(), name="Test", phone="+15555550100", timezone="UTC"),
        partner=None,
        triggering_message_ids=[],
        bot_id=bot_id,
        primary_topic_id=uuid4(),
        primary_topic_slug=spec.primary_topic_slug,
        current_step="read",
        bot_spec=spec,
    )
    return _step_allowed(ctx)


def test_nav_search_tools_are_registered_readable_and_proactively_described() -> None:
    exclusive_tools = set().union(*BOT_EXCLUSIVE_TOOLS.values())

    assert NEW_NAV_AND_SEARCH_TOOLS <= set(TOOL_DISPATCH)
    assert NEW_NAV_AND_SEARCH_TOOLS <= set(TOOL_DESCRIPTIONS)
    assert NEW_NAV_AND_SEARCH_TOOLS <= READ_PHASE_TOOLS
    assert NEW_NAV_AND_SEARCH_TOOLS.isdisjoint(exclusive_tools)

    for tool_name in NEW_NAV_AND_SEARCH_TOOLS:
        assert "hot-context gist" in TOOL_DESCRIPTIONS[tool_name], tool_name


def test_mediator_and_solo_bots_keep_nav_search_tools_plus_search_messages() -> None:
    expected = NEW_NAV_AND_SEARCH_TOOLS | {"search_messages"}

    for bot_id in ("mediator", "tante_rosi", "hector", "habits", "coach"):
        allowed = _step_allowed_for(bot_id)
        missing = expected - allowed
        assert not missing, f"{bot_id} lost read-tool exposure: {missing}"


def test_recent_activity_stays_excluded_from_solo_and_coach_bots() -> None:
    for bot_id in ("coach", "tante_rosi", "hector", "habits"):
        assert "recent_activity" not in _step_allowed_for(bot_id), bot_id
