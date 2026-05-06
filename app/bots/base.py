"""Bot profile contract for the shared agentic runner."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.models.user import User
from app.services.turn_plan import TurnPlan, TurnStep

PromptRenderer = Callable[..., str]


@dataclass(frozen=True)
class BotSpec:
    """All bot-specific choices consumed by the common adaptive turn runner."""

    bot_id: str
    prompt_renderer: PromptRenderer
    step_instructions: dict[TurnStep, str]
    skeleton_overrides: dict[str, list[TurnStep]] | None = None

    def render_system_prompt(
        self,
        *,
        assistant_name: str,
        user: User,
        partner: User,
        prompt_version: str,
    ) -> str:
        return self.prompt_renderer(
            assistant_name,
            user.name,
            partner.name,
            prompt_version=prompt_version,
            onboarding_state=user.onboarding_state,
            current_user_sharing_default=user.cross_thread_sharing_default,
            partner_sharing_default=partner.cross_thread_sharing_default,
        )

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
                    f"context={trigger_metadata.get('context', {})}."
                    f"{pacing_seed} "
                    f"{orient_header}\n\n"
                    f"Turn plan:\n{plan.render_checklist()}\n\n"
                    f"Current step instruction: {self.step_instructions[plan.current]}"
                ),
            }
        ]

    def build_step_transition_message(
        self,
        *,
        plan: TurnPlan,
        sent_summary: str | None = None,
    ) -> dict[str, Any]:
        parts = [
            f"Next step: {plan.current}.",
            "Current task: execute only the highlighted step. If nothing is needed, return an empty assistant response with no tool calls.",
            f"Turn plan:\n{plan.render_checklist()}",
        ]
        if sent_summary is not None and plan.current in {"record", "schedule"}:
            parts.append(sent_summary)
        parts.append(f"Current step instruction: {self.step_instructions[plan.current]}")
        return {
            "role": "user",
            "content": "\n\n".join(parts),
        }
