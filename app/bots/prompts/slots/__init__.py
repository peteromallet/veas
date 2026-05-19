"""Slots package — each submodule registers one ``PromptSlot`` at import time.

Imported by ``app.bots.prompts.registry`` at the bottom of the file to
populate the module-level ``_SLOTS`` list.  Submodules MUST be import-safe:
no database calls, no environment-dependent state at import time.

Import order is alphabetical; stable and predictable.
"""

from app.bots.prompts.slots import (  # noqa: F401
    adherence_board_rules,
    body_image_eating_safety,
    commitment_flow_rules,
    knowledge_primitives_rules,
    partner_nudge,
    reminders_bundling,
    reply_discipline,
    scheduling,
)
