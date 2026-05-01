"""Per-turn context shared by the agentic loop and tool implementations."""

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from app.models.user import User


@dataclass
class TurnContext:
    turn_id: UUID
    pool: Any
    user: User
    partner: User
    triggering_message_ids: list[UUID]
    phase: Literal["read", "write"] = "read"
    trigger_charge: str | None = None
    explicit_partner_alert_requested: bool = False


async def partner_of(pool: Any, user: User) -> User:
    rows = await pool.fetch(
        """
        SELECT id, name, phone, timezone
        FROM users
        WHERE id <> $1
        """,
        user.id,
    )
    if len(rows) != 1:
        raise ValueError(f"expected exactly one partner for user {user.id}, found {len(rows)}")
    row = rows[0]
    return User(id=row["id"], name=row["name"], phone=row["phone"], timezone=row["timezone"])
