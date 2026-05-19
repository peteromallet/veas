"""Back-compat re-export of the partner-nudge prompt slot.

The canonical partner-nudge slot body lives in
``app/bots/prompts/slots/partner_nudge.py`` and is registered in the
prompt registry.  This module resolves the named slot from the registry
so that existing importers continue to work without modification.

The autonomous-judgment draft (``_AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT``)
ships INERT — present in this file for future iteration but mounted by
NO renderer in this megaplan (invariant 6).
"""

from __future__ import annotations

from app.bots.prompts.registry import slots_for

import app.bots.prompts.slots  # noqa: F401  — populate registry


def _resolve_slot(name: str) -> str:
    """Return the body of the registry slot *name*."""
    for slot in slots_for("mediator"):
        if slot.name == name:
            return slot.body
    return ""


PARTNER_NUDGE_PROMPT_SLOT = _resolve_slot("partner_nudge")


# DRAFT — NOT MOUNTED. Autonomous bot-judgment nudges are intentionally
# unreachable in this release (invariant 6, SD-006). This text is here
# for the next iteration after we observe explicit-request usage; a
# feature flag will gate it.
_AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT = """\
DRAFT — not mounted.

You may also schedule a partner check-in on your own judgment when:
- the user has been carrying an asymmetric care load for the partner;
- there has been long silence near a significant event the partner
  would want to know about;
- distress in the user's thread would benefit from looping in the
  partner, and the user has not yet asked.

All the same hard-blocks apply: `no_dyad_partner`, recipient `opt_out`,
recipient `pending`. Set `source='bot_judgment'`. Be conservative —
prefer waiting for an explicit request over a marginal autonomous nudge.
""".strip()


__all__ = [
    "PARTNER_NUDGE_PROMPT_SLOT",
    "_AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT",
]
