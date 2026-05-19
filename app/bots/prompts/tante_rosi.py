"""Tante Rosi persona prompt — pregnancy coach bot.

Voice: plain-spoken, warm, careful. German by default; matches the
user's language when they clearly write in another, with a slight bias
toward German when ambiguous. No saccharine endearments, no
forced cheer. Care comes through in attention, not in decoration.

Medical defer is always-on: clinical questions go to a Hebamme / Ärztin.
Eight red flags trigger immediate escalation. Loss is handled directly
and without forward-momentum framing.
"""

from __future__ import annotations

from typing import Any

from app.bots.prompts.profiles.tante_rosi import PROFILE
from app.bots.prompts.profile import render_profile
from app.services.open_asks import OpenAsk

TANTE_ROSI_PROMPT_VERSION = "v1"


def render_system_prompt(
    assistant_name: str = "Tante Rosi",
    user_name: str = "",
    *,
    prompt_version: str = TANTE_ROSI_PROMPT_VERSION,
    onboarding_state: str | None = None,
    partner_share: str | None = None,
    partner_sharing_state: str | None = None,
    **kwargs: Any,
) -> str:
    """Render the Tante Rosi system prompt.

    Accepts **kwargs so dyad-shaped kwargs (partner, partner_partner_share)
    forwarded by BotSpec.render_system_prompt are silently ignored — Rosi
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


ASKS = [
    OpenAsk(
        key="pregnancy_edd",
        open_if=lambda state: state.get("pregnancy_edd") is None,
        example=(
            "Glückwunsch. Damit ich dich gut begleiten kann — weißt du "
            "schon deinen Entbindungstermin, oder wann deine letzte "
            "Periode war?"
        ),
        resolves_with="set_pregnancy_edd",
    ),
    OpenAsk(
        key="partner_share",
        open_if=lambda state: bool(state.get("has_partner"))
        and state.get("partner_share") is None,
        example=(
            "Willst du, dass ich {partner_name} ab und zu sage, wie's "
            "dir geht, oder lieber nicht?"
        ),
        resolves_with="set_partner_sharing",
    ),
]
