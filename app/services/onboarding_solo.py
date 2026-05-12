"""Solo onboarding state manager (Sprint 5).

Idempotent first-contact renderer per §10.2 of architecture doc. Uses upsert
on mediator.user_bot_state to prevent the simultaneous-first-message race
(§16.6 risk).

Note: The user_bot_state table (migration 0022) already has an
onboarding_state column (TEXT NOT NULL DEFAULT 'pending') and a
PRIMARY KEY (user_id, bot_id) — so no precursor migration is needed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


async def ensure_onboarding_state(pool: Any, *, user_id: UUID, bot_id: str) -> str:
    """Ensure a row exists in mediator.user_bot_state and return the
    onboarding_state.

    Idempotent: if the row already exists, the upsert leaves
    onboarding_state unchanged and returns the current value. If the row
    is missing, it is created with onboarding_state='pending'.

    Schema-qualified as 'mediator' per lesson #5.
    """
    row = await pool.fetchrow(
        """\
        INSERT INTO mediator.user_bot_state (user_id, bot_id, onboarding_state, updated_at)
        VALUES ($1, $2, 'pending', now())
        ON CONFLICT (user_id, bot_id) DO UPDATE
        SET updated_at = user_bot_state.updated_at
        RETURNING onboarding_state
        """,
        user_id,
        bot_id,
    )
    if row is None:
        raise RuntimeError(
            f"ensure_onboarding_state: upsert returned no row for "
            f"user_id={user_id} bot_id={bot_id}"
        )
    return row["onboarding_state"]


def render_first_contact(topic_display_name: str) -> str:
    """Return a short first-contact greeting for the solo bot.

    The caller is responsible for weaving this into the actual outbound
    message. This is a plain-text suggestion, not an auto-send.
    """
    return (
        f"Hi — I'm your {topic_display_name} reflection coach. "
        "I'm here to help you think through work, career questions, "
        "patterns you're noticing, and what you want next. Not a "
        "therapist — more like a thoughtful thinking partner. "
        "You can start wherever you like."
    )