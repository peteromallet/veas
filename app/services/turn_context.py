"""Per-turn context shared by the agentic loop and tool implementations."""

from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from uuid import UUID
from datetime import datetime

from app.models.user import User
from app.services.turn_plan import TurnPlan, TurnStep, make_turn_plan

PacedSendKind = Literal["final", "incremental_first", "incremental_next"]
BeforePacedSend = Callable[..., Awaitable[None]]


@dataclass
class TurnContext:
    turn_id: UUID
    pool: Any
    user: User
    partner: User
    triggering_message_ids: list[UUID]
    current_step: TurnStep = "respond"
    turn_plan: TurnPlan = field(default_factory=lambda: make_turn_plan("quick_reply"))
    tool_call_log: list[str] = field(default_factory=list)
    trigger_charge: str | None = None
    explicit_partner_alert_requested: bool = False
    turn_started_at: datetime | None = None
    incremental_sending_enabled: bool = False
    protected_owner_ids: list[UUID] | None = None
    send_typing_indicator: bool = True
    before_paced_send: BeforePacedSend | None = None
    sent_message_parts: list[dict[str, Any]] | None = None
    hot_context_rendered: str | None = None
    trigger_metadata: dict[str, Any] = field(default_factory=dict)


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
