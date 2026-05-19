"""Versioned solo system prompts for the solo bot runner (Sprint 5).

Mirrors the mediator's prompts.py pattern but for a single-user reflection
coach: no partner placeholder, no bridges, no in-person redirect, no dyadic
crisis escalation gate.
"""

from app.bots.prompts.profiles.coach import PROFILE
from app.bots.prompts.profile import render_profile
from app.services.open_asks import OpenAsk

SOLO_SYSTEM_PROMPT_VERSION = "v1"


def render_solo_system_prompt(
    assistant_name: str,
    user_name: str,
    *,
    prompt_version: str = SOLO_SYSTEM_PROMPT_VERSION,
    onboarding_state: str | None = None,
    partner_share: str | None = None,
    partner_sharing_state: str | None = None,
    topic_display_name: str = "career",
    **kwargs: object,
) -> str:
    """Render the solo system prompt for the coach bot.

    Accepts **kwargs so the partner kwarg from BotSpec.render_system_prompt
    is silently ignored (T5 base.py guard also prevents AttributeError at
    the caller level).
    """
    del prompt_version, onboarding_state, partner_sharing_state
    return render_profile(
        PROFILE,
        assistant_name=assistant_name,
        user_name=user_name,
        partner_share=partner_share,
        topic_display_name=topic_display_name,
        **{k: v for k, v in kwargs.items() if v is not None},
    )


ASKS = [
    OpenAsk(
        key="partner_share",
        open_if=lambda state: bool(state.get("has_partner"))
        and state.get("partner_share") is None,
        example=(
            "Before we go further, would you like me to share small, "
            "carefully chosen context from this thread with {partner_name}, "
            "or keep this fully private?"
        ),
        resolves_with="set_partner_sharing",
    ),
]
