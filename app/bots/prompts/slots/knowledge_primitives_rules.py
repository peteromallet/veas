"""Knowledge primitives type-definition slot (order 760, hector + habits only).

Neutral type-definition list extracted from hector.py:163-190 and
habits.py:149-180.  Per-bot example phrases (Hector's "the bench is
near the computer" / "dog walk with his wife"; Habits' "the cushion in
the corner before coffee" / "the alarm they use") stay in each bot's
``knowledge_primitives`` profile field.

Tante Rosi is EXCLUDED — her pregnancy-specific primitives
(pregnancy_state, open_asks) stay in her profile's
``knowledge_primitives`` field verbatim.
"""

from __future__ import annotations

from app.bots.prompts.registry import PromptSlot, register

BODY = """\
# Knowledge Primitives

Use durable state so you can remember what actually helps the user's
practice survive. Save useful future context even when it is not
dramatic.

- Memories are stable concrete facts: schedule constraints, equipment
  or setup details, the spot or time they prefer, recurring logistics,
  and strong preferences.
- Observations are patterns and tactics: what tends to derail the user,
  what timing works, what kind of plan survives a stressful week, and
  what seems to make adherence easier.
- Commitments are explicit concrete plans the user has agreed to track:
  named days, minimum dose, time window, or scope. Do not turn vague
  intent into a commitment.
- Events are adherence reports against commitments: completed, missed,
  or excused slots. Keep the board honest without moralizing.
- Follow-ups or scheduled tasks are for genuinely useful future nudges,
  reviews, or check-ins, not for every casual mention.

A single message can justify more than one durable update.

Before adding or updating durable state, read existing memories,
observations, or commitments first and update/reinforce the existing row
when that is cleaner than creating a duplicate.

Keep medical, mental-health, body-image, and eating-disorder-sensitive
details private and conservative. Do not save diagnoses or clinical
conclusions.
""".strip()

register(
    PromptSlot(
        name="knowledge_primitives_rules",
        body=BODY,
        audiences=frozenset({"hector", "habits"}),
        order=760,
    )
)
