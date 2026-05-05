"""Bot profile contract for the shared agentic runner."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.models.user import User

PromptRenderer = Callable[..., str]


@dataclass(frozen=True)
class BotSpec:
    """All bot-specific choices consumed by the common two-phase runner."""

    bot_id: str
    prompt_renderer: PromptRenderer
    read_phase_tools: frozenset[str]
    write_phase_tools: frozenset[str]
    phase_a_instruction: str
    phase_b_instruction: str

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
                    f"context={trigger_metadata.get('context', {})}."
                    f"{pacing_seed} "
                    f"{self.phase_a_instruction}"
                ),
            }
        ]

    def build_phase_b_seed_message(
        self,
        *,
        sent_summary: str,
    ) -> dict[str, Any]:
        return {
            "role": "user",
            "content": f"{sent_summary}. {self.phase_b_instruction}",
        }
