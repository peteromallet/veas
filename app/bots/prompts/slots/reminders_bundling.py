"""Reminders bundling slot (order 850, all bots).

Body matches decision 14 of the design brief verbatim.  Teaches bots to
prefer folding new reminders into existing ones based on judgment rather
than fixed time windows.
"""

from __future__ import annotations

from app.bots.prompts.registry import ALL_BOTS, PromptSlot, register

BODY = """\
Before booking a new reminder, look at the `Upcoming reminders` section
in the hot context and consider running `list_all_reminders` for the
full picture. Ask: could the new intent ride on an existing reminder —
by broadening its brief, by folding it into a morning or evening
check-in that already covers several things, or by updating its time?
Prefer one richer reminder that does several jobs over many narrow ones.
This is judgment, not a fixed time window: two reminders ten minutes
apart may legitimately be separate, while two reminders six hours apart
may legitimately belong together. When you bundle, use
`update_scheduled_task` for agent-managed tasks or
`update_scheduled_checkin` for user-facing check-ins, and tell the user
concisely what you folded in.
""".strip()

register(
    PromptSlot(
        name="reminders_bundling",
        body=BODY,
        audiences=ALL_BOTS,
        order=850,
    )
)
