"""Back-compat re-export of the scheduling prompt slot.

The canonical scheduling slot body lives in
``app/bots/prompts/slots/scheduling.py`` and is registered in the
prompt registry.  This module resolves the named slot from the registry
so that existing importers continue to work without modification.
"""

from __future__ import annotations

from app.bots.prompts.registry import slots_for

import app.bots.prompts.slots  # noqa: F401  — populate registry


def _resolve_slot(name: str) -> str:
    """Return the body of the registry slot *name*.

    Uses ``slots_for('mediator')`` because the scheduling slot is
    audience ``ALL_BOTS`` — any bot id works.
    """
    for slot in slots_for("mediator"):
        if slot.name == name:
            return slot.body
    return ""


SCHEDULING_CAPABILITY_PROMPT_SLOT = _resolve_slot("scheduling")

__all__ = ["SCHEDULING_CAPABILITY_PROMPT_SLOT"]
