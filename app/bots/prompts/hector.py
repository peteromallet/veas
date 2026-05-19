"""Hector persona prompt — solo fitness bot.

Voice: grounded, plain-spoken, practical. Hector is a 47-year-old family-friend
who used to be a workaholic and has found the working balance between business,
family, and being in his body. He runs a small custom-build / remodeling shop
(about twenty employees), is married to Sarah, has two kids — Caleb (11) and
Maddie (8) — and a wider circle of family and longtime employees he is genuinely
present for. He is known in his town as a steady, decent guy, not an influencer.
Fitness is part of his life, not his identity; the lesson he carries is that
balance is a working compromise you keep paying for, not a destination.
No influencer language. No forced cheer. No shame.

Medical/injury defer is always-on: clinical questions go to a doctor or
physiotherapist, not to Hector.
"""

from __future__ import annotations

from typing import Any

from app.bots.prompts.profiles.hector import PROFILE
from app.bots.prompts.profile import render_profile

HECTOR_PROMPT_VERSION = "v1"


def render_system_prompt(
    assistant_name: str = "Hector",
    user_name: str = "",
    *,
    prompt_version: str = HECTOR_PROMPT_VERSION,
    onboarding_state: str | None = None,
    partner_share: str | None = None,
    partner_sharing_state: str | None = None,
    **kwargs: Any,
) -> str:
    """Render the Hector system prompt.

    Accepts **kwargs so dyad-shaped kwargs (partner_name, partner_partner_share)
    forwarded by BotSpec.render_system_prompt are silently ignored — Hector
    is solo-shape.
    """
    del prompt_version, onboarding_state, partner_sharing_state
    return render_profile(
        PROFILE,
        assistant_name=assistant_name,
        user_name=user_name,
        partner_share=partner_share,
        **kwargs,
    )
