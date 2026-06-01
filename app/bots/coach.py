"""Solo 'coach' bot profile (career topic).

S4 pre-flight only: BotSpec wired but the prompt renderer + hot context code
live in S5. The spec is registered in BOT_SPECS only when STAGING env truthy.

TODO (T16 deferred): staging seed verification and consenting prod user UUID
are operator-held; prod-promotion migration is gated on these per locked
decision #6 — see feedback.md.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.bots.base import BotSpec, ReadScopes, WriteScopes
from app.services.prompts_solo import render_solo_system_prompt


def _coach_prompt_renderer(
    assistant_name: str,
    user_name: str,
    partner_name: str | None = None,
    *,
    prompt_version: str = "v1",
    onboarding_state: str | None = None,
    current_user_partner_share: str | None = None,
    partner_partner_share: str | None = None,
    current_user_partner_sharing_state: str | None = None,
    partner_partner_sharing_state: str | None = None,
    **kwargs: Any,
) -> str:
    """Coach prompt renderer — delegates to the solo system prompt.

    Accepts partner_name, partner_partner_share, and partner (via
    **kwargs) from BotSpec.render_system_prompt but ignores them.  The
    solo renderer has no dyadic concepts.
    """
    return render_solo_system_prompt(
        assistant_name,
        user_name,
        prompt_version=prompt_version,
        onboarding_state=onboarding_state,
        partner_share=current_user_partner_share,
        partner_sharing_state=current_user_partner_sharing_state,
        topic_display_name="career",
    )


_MIN_STEP_INSTRUCTIONS = {
    "read": "Read step (coach S4 stub).",
    "consult": "Consult step (coach S4 stub).",
    "respond": "Respond step (coach S4 stub).",
    "record": "Record step (coach S4 stub).",
    "schedule": "Schedule step (coach S4 stub).",
    "done": "Done step (coach S4 stub).",
}


def build_coach_spec() -> BotSpec:
    """Build the coach BotSpec.

    Lazy import of TOOL_DISPATCH avoids a circular import (registry imports
    write_tools, which doesn't import coach).  The tool_allowlist is the
    full dispatch table minus dyad-only tools (bridge candidates, partner
    escalation, and set_topic_status — per §16.6 locked decisions).
    """
    from app.services.tools.registry import TOOL_DISPATCH, HECTOR_ONLY_TOOLS

    return BotSpec(
        bot_id="coach",
        prompt_renderer=_coach_prompt_renderer,
        step_instructions=_MIN_STEP_INSTRUCTIONS,
        display_name="Coach",
        primary_topic_slug="career",
        participants_shape="solo",
        read_scopes=ReadScopes(topics=frozenset({"career"})),
        write_scopes=WriteScopes(topics=frozenset({"career"})),
        cross_topic_policy="peek",
        tool_allowlist=frozenset(TOOL_DISPATCH.keys())
        - frozenset(
            {
                "set_topic_status",
                "create_bridge_candidate",
                "update_bridge_candidate",
                "send_bridge_candidate",
                "list_bridge_candidates",
                "escalate_to_partner",
                "recent_activity",
                # Pregnancy tools: coach is career, not pregnancy.
                "set_pregnancy_edd",
                "correct_pregnancy_edd",
                "end_pregnancy",
            }
        )
        - HECTOR_ONLY_TOOLS,
        bot_spec_version="1.2.0",
    )
