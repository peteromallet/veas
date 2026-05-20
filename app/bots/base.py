"""Bot profile contract for the shared agentic runner."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.models.user import User
from app.services.turn_plan import TurnPlan, TurnStep

PromptRenderer = Callable[..., str]


@dataclass(frozen=True)
class ReadScopes:
    """Reading permissions for a bot across topics (§6).

    topics: set of topic slugs the bot may read directly. The sentinel
    'own' means the bot's primary topic (resolved at gate time against
    ctx.primary_topic_slug). 'all' means cross-topic reads are allowed.
    """

    topics: frozenset[str] = field(default_factory=lambda: frozenset({"own"}))
    allow_cross_topic_peek: bool = False
    allow_cross_topic_status_injection: bool = False


@dataclass(frozen=True)
class WriteScopes:
    """Writing permissions for a bot across topics (§6).

    topics: set of topic slugs the bot may write to. 'own' resolves to
    primary_topic_slug. 'all' is reserved for cross-topic writes (S6).
    """

    topics: frozenset[str] = field(default_factory=lambda: frozenset({"own"}))
    require_reason_for_cross_topic: bool = False


_ALLOWED_PROVIDERS: frozenset[str] = frozenset({"anthropic", "deepseek"})


@dataclass(frozen=True)
class BotSpec:
    """All bot-specific choices consumed by the common adaptive turn runner."""

    bot_id: str
    prompt_renderer: PromptRenderer
    step_instructions: dict[TurnStep, str]
    skeleton_overrides: dict[str, list[TurnStep]] | None = None
    # Sprint 1 new optional fields with mediator-shaped defaults
    display_name: str = "Mediator"
    primary_topic_slug: str = "relationship"
    participants_shape: str = "dyad"
    read_scopes: ReadScopes = field(default_factory=ReadScopes)
    write_scopes: WriteScopes = field(default_factory=WriteScopes)
    cross_topic_policy: str | None = None
    tool_allowlist: frozenset[str] | None = None
    bot_spec_version: str = "1.1.0"
    hot_context_builder_version: str = "1.0.0"
    tool_schema_version: str = "1.0.0"
    # Per-bot LLM provider chain (primary, fallbacks...).
    provider_chain: tuple[str, ...] = ("deepseek", "anthropic")

    def __post_init__(self) -> None:
        for entry in self.provider_chain:
            if entry not in _ALLOWED_PROVIDERS:
                raise ValueError(
                    f"BotSpec.provider_chain contains unsupported provider "
                    f"{entry!r}; allowed: {sorted(_ALLOWED_PROVIDERS)}"
                )
        if not self.provider_chain:
            raise ValueError("BotSpec.provider_chain must not be empty")

    def render_system_prompt(
        self,
        *,
        assistant_name: str,
        user: User,
        partner: User | None,
        prompt_version: str,
        current_user_partner_share: str | None = None,
        partner_partner_share: str | None = None,
        current_user_partner_sharing_state: str | None = None,
        partner_partner_sharing_state: str | None = None,
    ) -> str:
        partner_name = partner.name if partner is not None else None
        return self.prompt_renderer(
            assistant_name,
            user.name,
            partner_name,
            prompt_version=prompt_version,
            onboarding_state=user.onboarding_state,
            current_user_partner_share=current_user_partner_share,
            partner_partner_share=partner_partner_share,
            current_user_partner_sharing_state=current_user_partner_sharing_state,
            partner_partner_sharing_state=partner_partner_sharing_state,
            partner=partner,
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
        parts.append(
            f"Current step instruction: {self.step_instructions[plan.current]}"
        )
        return {
            "role": "user",
            "content": "\n\n".join(parts),
        }
