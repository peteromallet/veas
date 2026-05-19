"""Reply-discipline slot (order 1000, all bots).

Universal closing reminder: one question per reply, keep replies short.
Does NOT replace any bot's richer Output Style guidance — mediator and
solo_coach keep their full Output Style sections in their profiles.
"""

from __future__ import annotations

from app.bots.prompts.registry import ALL_BOTS, PromptSlot, register

BODY = """\
- One question per reply, maximum. Do not interview.
- Keep replies short by default. Longer only when there is substance to
  say.
""".strip()

register(
    PromptSlot(
        name="reply_discipline",
        body=BODY,
        audiences=ALL_BOTS,
        order=1000,
    )
)
