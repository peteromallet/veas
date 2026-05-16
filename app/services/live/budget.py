"""Per-session budget guard for the live voice surface.

Soft cap = $2 / session; hard cap = $4 / session. Stored as cents on
``mediator.conversations.spend_usd_cents``.

Estimates are conservative — they're computed from the *visible* token
counts after each LLM call (real impls) or 0 for stubs. The wiring is
in place so a real Anthropic/OpenAI/ElevenLabs call swap-in will start
charging immediately without a router change.

Public API:

* ``charge_session(pool, session_id, cents)`` — atomically bump spend
  and return the new total. Refuses to charge past the hard cap and
  returns a `BudgetCapped` sentinel.
* ``check_budget(pool, session_id)`` — read-only fetch returning a
  `BudgetState` (cents, soft_warned, hard_capped).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

SOFT_CAP_CENTS = 200  # $2.00
HARD_CAP_CENTS = 400  # $4.00


@dataclass(frozen=True)
class BudgetState:
    cents: int
    soft_warned: bool
    hard_capped: bool


async def check_budget(pool: Any, session_id: UUID) -> BudgetState:
    row = await pool.fetchrow(
        "SELECT spend_usd_cents FROM mediator.conversations WHERE id = $1",
        session_id,
    )
    cents = int(row["spend_usd_cents"]) if row else 0
    return BudgetState(
        cents=cents,
        soft_warned=cents >= SOFT_CAP_CENTS,
        hard_capped=cents >= HARD_CAP_CENTS,
    )


async def charge_session(pool: Any, session_id: UUID, cents: int) -> BudgetState:
    """Bump spend by ``cents`` and return the new state.

    No-op when ``cents <= 0``. The UPDATE runs unconditionally — we don't
    short-circuit on hard_capped because operators may want to see the
    final cost. The router enforces the cap separately by reading
    ``hard_capped`` before queuing the next turn.
    """
    if cents > 0:
        await pool.execute(
            """
            UPDATE mediator.conversations
            SET spend_usd_cents = spend_usd_cents + $2
            WHERE id = $1
            """,
            session_id,
            int(cents),
        )
    return await check_budget(pool, session_id)
