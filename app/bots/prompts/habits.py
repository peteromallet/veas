"""Habits persona prompt — solo habits bot.

Voice: grounded, plain-spoken, practical. The habits bot is a steady,
attentive companion for whatever practice the user has chosen to keep —
meditation, screens, sleep hygiene, journaling, hydration, reading, anything
that survives or doesn't survive a real life. Less of a specific persona
than Hector: identity comes from steadiness and attention, not biography.

No influencer language. No forced cheer. No shame.

Clinical and mental-health defer is always-on: if a practice (e.g.
meditation) surfaces something the user should bring to a professional,
defer to a professional.
"""

from __future__ import annotations

from typing import Any

from app.bots.prompts.profiles.habits import PROFILE
from app.bots.prompts.profile import render_profile

HABITS_PROMPT_VERSION = "v1"


def render_system_prompt(
    assistant_name: str = "Habits",
    user_name: str = "",
    *,
    prompt_version: str = HABITS_PROMPT_VERSION,
    onboarding_state: str | None = None,
    partner_share: str | None = None,
    partner_sharing_state: str | None = None,
    **kwargs: Any,
) -> str:
    """Render the Habits system prompt.

    Accepts **kwargs so dyad-shaped kwargs (partner_name, partner_partner_share)
    forwarded by BotSpec.render_system_prompt are silently ignored — the
    habits bot is solo-shape.
    """
    del prompt_version, onboarding_state, partner_sharing_state
    return render_profile(
        PROFILE,
        assistant_name=assistant_name,
        user_name=user_name,
        partner_share=partner_share,
        **kwargs,
    )
