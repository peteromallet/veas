"""Registry of bot profiles available to the shared runner."""

from __future__ import annotations

from app.bots.base import BotSpec
from app.bots.mediator import MEDIATOR_BOT

BOT_SPECS: dict[str, BotSpec] = {
    MEDIATOR_BOT.bot_id: MEDIATOR_BOT,
}


class UnknownBotSpec(ValueError):
    pass


def get_bot_spec(bot_id: str) -> BotSpec:
    try:
        return BOT_SPECS[bot_id]
    except KeyError as exc:
        known = ", ".join(sorted(BOT_SPECS))
        raise UnknownBotSpec(f"unknown bot spec: {bot_id}; known specs: {known}") from exc
