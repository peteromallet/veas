"""Bot profiles for the shared agentic runner."""

from app.bots.base import BotSpec
from app.bots.registry import BOT_SPECS, UnknownBotSpec, get_bot_spec

__all__ = ["BOT_SPECS", "BotSpec", "UnknownBotSpec", "get_bot_spec"]
