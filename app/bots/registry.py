"""Registry of bot profiles available to the shared runner."""

from __future__ import annotations

import logging
from typing import Any

from app.bots.base import BotSpec, ReadScopes, WriteScopes
from app.bots.mediator import MEDIATOR_BOT

logger = logging.getLogger(__name__)

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


async def populate_mediator_spec_from_db(pool: Any) -> None:
    """Read mediator display_name from the bots table and rebuild MEDIATOR_BOT.

    All scope/topic/version fields are hardcoded mediator defaults.
    Only display_name comes from the database — the bots table is intentionally
    thin per §3. On miss, warns and keeps code defaults unchanged.
    """
    try:
        row = await pool.fetchrow(
            "SELECT display_name FROM bots WHERE id = 'mediator'"
        )
    except Exception:
        logger.warning(
            "populate_mediator_spec_from_db: could not query bots table — "
            "keeping module-level defaults",
            exc_info=True,
        )
        return
    if row is None:
        logger.warning(
            "populate_mediator_spec_from_db: no mediator row in bots table — "
            "keeping module-level defaults"
        )
        return

    display_name = row["display_name"]
    # Reconstruct MediatorBotSpec with DB display_name + hardcoded defaults
    from app.bots.mediator import MediatorBotSpec, MEDIATOR_STEP_INSTRUCTIONS

    rebuilt = MediatorBotSpec(
        bot_id="mediator",
        prompt_renderer=MEDIATOR_BOT.prompt_renderer,
        step_instructions=MEDIATOR_STEP_INSTRUCTIONS,
        skeleton_overrides=MEDIATOR_BOT.skeleton_overrides,
        display_name=display_name,
        primary_topic_slug="relationship",
        participants_shape="dyad",
        read_scopes=ReadScopes(),
        write_scopes=WriteScopes(),
        # Preserve version fields so anything the in-code MEDIATOR_BOT
        # already had set survives the rebuild.
        bot_spec_version=MEDIATOR_BOT.bot_spec_version,
        hot_context_builder_version=MEDIATOR_BOT.hot_context_builder_version,
        tool_schema_version=MEDIATOR_BOT.tool_schema_version,
    )
    BOT_SPECS["mediator"] = rebuilt
    logger.info("populate_mediator_spec_from_db: mediator spec updated (display_name=%s)", display_name)
