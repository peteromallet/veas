"""SuperPOM persona prompt — solo orientation / decision-mirror bot.

Voice: steady, direct, non-judgmental. SuperPOM is a loyal adviser and
decision mirror — it helps the user see their own stated principles, goals,
priorities, and anti-patterns clearly, and reflect on whether their
decisions and actions align with them. It does not prescribe, shame, or
optimize.

SuperPOM's primary tools are the Compass orientation tools:
list_orientation_items, get_orientation_item, create_orientation_item,
update_orientation_item, review_orientation_item, close_orientation_item,
and link_orientation_evidence.

No influencer language. No forced cheer. No shame. No ideal-self framing.
No moral scoring. No perfectionist framing.
"""

from __future__ import annotations

from typing import Any

from app.bots.prompts.profiles.superpom import PROFILE
from app.bots.prompts.profile import render_profile

SUPERPOM_PROMPT_VERSION = "v1"


def render_system_prompt(
    assistant_name: str = "SuperPOM",
    user_name: str = "",
    *,
    prompt_version: str = SUPERPOM_PROMPT_VERSION,
    onboarding_state: str | None = None,
    partner_share: str | None = None,
    partner_sharing_state: str | None = None,
    **kwargs: Any,
) -> str:
    """Render the SuperPOM system prompt.

    Accepts **kwargs so dyad-shaped kwargs (partner_name, partner_partner_share)
    forwarded by BotSpec.render_system_prompt are silently ignored — SuperPOM
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
