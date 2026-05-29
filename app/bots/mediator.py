"""Mediator bot profile.

This module is the instance-specific home for the current relationship
mediation bot: its prompt renderer, tool surface, and phase instructions.
The agentic runner should stay generic and consume this profile.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from app.bots.base import BotSpec, ReadScopes, WriteScopes
from app.bots.ids import MEDIATOR_BOT_ID
from app.services.prompts import render_system_prompt
from app.services.turn_plan import TurnPlan


class MediatorBotSpec(BotSpec):
    def build_initial_seed(
        self,
        *,
        trigger_metadata: dict[str, Any],
        triggering_message_ids: list[UUID],
        charge: str | None,
        orient_header: str,
        plan: TurnPlan,
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
                    f"{orient_header}\n\n"
                    f"Turn plan:\n{plan.render_checklist()}\n\n"
                    f"Current step instruction: {self.step_instructions[plan.current]}"
                ),
            }
        ]


MEDIATOR_RESPOND_INSTRUCTION = (
    "Respond step: produce the user-facing response, a reaction directive, or silence. "
    "On Discord, prefer `send_message_part` whenever the response should "
    "feel like separate chat bubbles: explicit multi-message requests, short acknowledgement "
    "then deeper thought, or otherwise stacked lines. Send each intended bubble with its own "
    "`send_message_part` call, see whether it actually sent, and continue from the returned "
    "`sent_so_far`. Do not stream every thought or send process updates. If "
    "`send_message_part` returns `interrupted`, stop sending in this turn. "
    "After all intended `send_message_part` calls have been sent, return an empty assistant "
    "response; do not narrate the next step or summarize what you just sent. "
    "If a text reply would be unnecessary and a small acknowledgement is enough, "
    "you may use `search_emojis` and then produce exactly one `[react: emoji]` directive instead. "
    "If a reaction would naturally complement a short reply, put one `[react: emoji]` "
    "directive on its own line before or after the reply; the directive will not be shown to the user. "
    "If the user asks you to emoji react, use a `[react: emoji]` directive; do not claim "
    "Discord reactions are unavailable. "
    "If you intend to check in later, phrase it naturally in the user-facing response: "
    "say \"I'll check in with you then\", \"I'll check in tomorrow morning\", or "
    "\"I'll come back to this with you around 7\"; do not say \"I've scheduled that\", "
    "\"I scheduled a task\", \"I'll set a reminder\", or mention scheduling machinery. "
    "Do not include scratch notes, analysis of the message, tool/read decisions, or separators. "
    "If the user is building or editing a live-voice agenda, you may call "
    "`create_conversation_plan` or `update_conversation_plan` here to persist the "
    "agenda, then echo the resulting numbered list back via `send_message_part` as "
    "the spoken confirmation. Never call `create_conversation_plan` without explicit "
    "user confirmation of the numbered list."
)

MEDIATOR_RECORD_INSTRUCTION = (
    "Record step: record any state changes (memories, observations, distillations, theme updates, "
    "watch items, bridge candidates, style notes, feedback, OOB updates) that are justified by this turn. "
    "Read before durable writes when needed. "
    "If this turn has materially shifted the relationship's current state, call set_topic_status at most "
    "once with a short headline (≤80 chars) and optional body (≤300 chars). Otherwise omit it. "
    "If no durable update is justified, return an empty assistant response with no tool calls; the runner will advance automatically. "
    "Do not produce user-facing text."
)

MEDIATOR_SCHEDULE_INSTRUCTION = (
    "Schedule step: final optional follow-up check. Ask yourself whether there is anything genuinely useful "
    "to schedule as a follow-up or task. It is completely fine and often correct to do nothing. "
    "Use scheduling only when future check-ins or scheduled tasks are genuinely useful and not duplicative. "
    "If no schedule is needed, return an empty assistant response with no tool calls; the runner will advance automatically. "
    "Do not produce user-facing text."
)

MEDIATOR_STEP_INSTRUCTIONS = {
    "read": "Read step: gather only the context needed for this turn. If no extra context is needed, return an empty assistant response with no tool calls; the runner will advance automatically. Do not send user-facing text or write durable state.",
    "consult": "Consult step: use `consult_perspective` only when the user explicitly asked for a second opinion, critique, review, or another perspective; otherwise return an empty assistant response with no tool calls.",
    "respond": MEDIATOR_RESPOND_INSTRUCTION,
    "record": MEDIATOR_RECORD_INSTRUCTION,
    "schedule": MEDIATOR_SCHEDULE_INSTRUCTION,
    "done": "Done step: end the turn without additional user-facing text.",
}

MEDIATOR_BOT = MediatorBotSpec(
    bot_id=MEDIATOR_BOT_ID,
    prompt_renderer=render_system_prompt,
    step_instructions=MEDIATOR_STEP_INSTRUCTIONS,
    cross_topic_policy="peek",
    read_scopes=ReadScopes(
        topics=frozenset({"own"}),
        allow_cross_topic_peek=True,
        allow_cross_topic_status_injection=True,
    ),
    write_scopes=WriteScopes(
        topics=frozenset({"relationship"}),
        require_reason_for_cross_topic=True,
    ),
)
