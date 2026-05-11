"""Routing helpers — resolve bots, senders, and bindings.

NOT wired into inbound.py yet. Sprint 1 delivers the module + unit tests.
Sprint 2a wires resolve_bot into the inbound path.
"""

from __future__ import annotations

from typing import Any, NamedTuple
from uuid import UUID


class BindingResolution(NamedTuple):
    binding_id: UUID
    bot_id: str
    dyad_id: UUID | None
    user_id: UUID | None


async def resolve_bot(pool: Any, *, transport: str, address: str) -> str | None:
    """Map a transport+address pair to a bot_id via the channels table.

    Returns the bot_id of the channel that matches, or None if no channel found.
    """
    row = await pool.fetchrow(
        """
        SELECT bot_id
        FROM channels
        WHERE transport = $1 AND address = $2
        LIMIT 1
        """,
        transport,
        address,
    )
    return row["bot_id"] if row is not None else None


async def resolve_sender(pool: Any, *, transport: str, address: str) -> UUID | None:
    """Map a transport+address pair to a user_id via user_identities.

    Returns the user_id, or None if the identity is not registered.
    """
    row = await pool.fetchrow(
        """
        SELECT user_id
        FROM user_identities
        WHERE transport = $1 AND address = $2
        """,
        transport,
        address,
    )
    return row["user_id"] if row is not None else None


async def resolve_binding(pool: Any, *, bot_id: str, user_id: UUID) -> BindingResolution | None:
    """Find the binding between a bot and a user.

    Looks up bot_bindings joined with dyad_members to find the binding
    that links this bot to this user (either directly via user_id or through
    dyad membership). Returns the BindingResolution or None.

    The ORDER BY makes the result deterministic if multiple bindings ever
    match — pick the oldest (earliest created_at) and break ties on id.
    """
    row = await pool.fetchrow(
        """
        SELECT bb.id    AS binding_id,
               bb.bot_id,
               bb.dyad_id,
               bb.user_id
        FROM bot_bindings bb
        LEFT JOIN dyad_members dm ON dm.dyad_id = bb.dyad_id
        WHERE bb.bot_id = $1
          AND (bb.user_id = $2 OR dm.user_id = $2)
        ORDER BY bb.created_at, bb.id
        LIMIT 1
        """,
        bot_id,
        user_id,
    )
    if row is None:
        return None
    return BindingResolution(
        binding_id=row["binding_id"],
        bot_id=row["bot_id"],
        dyad_id=row["dyad_id"],
        user_id=row["user_id"],
    )