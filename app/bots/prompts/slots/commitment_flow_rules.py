"""Commitment-flow rules slot (order 780, hector + habits only).

Neutral rules extracted from hector.py:196-247 and habits.py:181-233.
Per-bot example quotes (Hector's "I am going to work out Monday to
Friday" / "What are we actually putting on the board this week:
workouts, food, or both?"; Habits' "I am going to meditate every
morning before coffee" / "daily sit, no phone after dinner, something
else?") stay in each bot's ``knowledge_primitives`` or ``custom_tail``
profile field.
"""

from __future__ import annotations

from app.bots.prompts.registry import PromptSlot, register

BODY = """\
# Commitment Flow

## When The User States A Plan

If the user states a concrete plan, call `create_commitment`. Log it
and confirm.

If the user states something vague, do NOT create a commitment. Ask one
practical clarifying question.

Create commitments only from concrete user plans. If the plan is vague,
ask before tracking.

## When The User Accepts A Proposed Plan

If you just proposed a concrete plan and the user accepts it, call
`list_commitments` to check for existing matches; if none exist, call
`create_commitment` with the agreed plan details and use the returned
`commitment_id`. Then acknowledge succinctly. Never invent a
`commitment_id` — always use the value returned by `create_commitment`
or `list_commitments`.

## When The User Reports Adherence

Call `log_event` against the relevant commitment. The reply can be
simple.

## Weekly Review

At week end, use adherence data to summarize and adjust.
""".strip()

register(
    PromptSlot(
        name="commitment_flow_rules",
        body=BODY,
        audiences=frozenset({"hector", "habits"}),
        order=780,
    )
)
