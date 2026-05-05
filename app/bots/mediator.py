"""Mediator bot profile.

This module is the instance-specific home for the current relationship
mediation bot: its prompt renderer, tool surface, and phase instructions.
The agentic runner should stay generic and consume this profile.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from app.bots.base import BotSpec
from app.services.prompts import render_system_prompt
from app.services.tools.registry import READ_PHASE_TOOLS, WRITE_PHASE_TOOLS


class MediatorBotSpec(BotSpec):
    def build_phase_a_seed(
        self,
        *,
        trigger_metadata: dict[str, Any],
        triggering_message_ids: list[UUID],
        charge: str | None,
    ) -> list[dict[str, Any]]:
        pacing_context = trigger_metadata.get("pacing")
        pacing_seed = (
            f" pacing={json.dumps(pacing_context, default=str)}."
            if pacing_context is not None
            else ""
        )
        return [
            {
                "role": "user",
                "content": (
                    f"Trigger: kind={trigger_metadata.get('kind', 'inbound')} "
                    f"ids={triggering_message_ids} charge={charge or 'routine'} "
                    f"context={json.dumps(trigger_metadata.get('context', {}), default=str)}."
                    f"{pacing_seed} "
                    f"{self.phase_a_instruction}"
                ),
            }
        ]


MEDIATOR_PHASE_A_INSTRUCTION = (
    "Phase A: read what you need, then produce the user-facing response. "
    "On Discord, prefer `send_message_part` during Phase A whenever the response should "
    "feel like separate chat bubbles: explicit multi-message requests, short acknowledgement "
    "then deeper thought, or otherwise stacked lines. Send each intended bubble with its own "
    "`send_message_part` call, see whether it actually sent, and continue from the returned "
    "`sent_so_far`. Do not stream every thought or send process updates. If "
    "`send_message_part` returns `interrupted`, stop sending in this turn. "
    "If a text reply would be unnecessary and a small acknowledgement is enough, "
    "you may use `search_emojis` and then produce exactly one `[react: emoji]` directive instead. "
    "If a reaction would naturally complement a short reply, put one `[react: emoji]` "
    "directive on its own line before or after the reply; the directive will not be shown to the user. "
    "If the user asks you to emoji react, use a `[react: emoji]` directive; do not claim "
    "Discord reactions are unavailable. "
    "Do not include scratch notes, analysis of the message, tool/read decisions, or separators."
)

MEDIATOR_PHASE_B_INSTRUCTION = (
    "Now record any state changes (memories, observations, distillations, theme updates, "
    "watch items, scheduled tasks) and optionally schedule, update, or cancel follow-ups. "
    "Do not produce user-facing text."
)

MEDIATOR_BOT = MediatorBotSpec(
    bot_id="mediator",
    prompt_renderer=render_system_prompt,
    read_phase_tools=frozenset(READ_PHASE_TOOLS),
    write_phase_tools=frozenset(WRITE_PHASE_TOOLS),
    phase_a_instruction=MEDIATOR_PHASE_A_INSTRUCTION,
    phase_b_instruction=MEDIATOR_PHASE_B_INSTRUCTION,
)
