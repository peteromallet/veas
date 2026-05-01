"""Small user model helpers shared by ingestion, debouncing, and recovery."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class User:
    id: UUID
    name: str
    phone: str
    timezone: str
    onboarding_state: str = "pending"


def _row_to_user(row: Any) -> User:
    onboarding_state = row["onboarding_state"] if "onboarding_state" in row else "pending"
    return User(
        id=row["id"],
        name=row["name"],
        phone=row["phone"],
        timezone=row["timezone"],
        onboarding_state=onboarding_state,
    )


async def fetch_user_by_id(pool: Any, user_id: UUID) -> User:
    row = await pool.fetchrow(
        "SELECT id, name, phone, timezone, onboarding_state FROM users WHERE id = $1",
        user_id,
    )
    return _row_to_user(row)


async def upsert_user(pool: Any, name: str, phone: str, default_tz: str) -> User:
    row = await pool.fetchrow(
        """
        INSERT INTO users (name, phone, timezone)
        VALUES ($1, $2, $3)
        ON CONFLICT (phone) DO UPDATE SET name = EXCLUDED.name
        RETURNING id, name, phone, timezone, onboarding_state
        """,
        name,
        phone,
        default_tz,
    )
    return _row_to_user(row)


async def claim_onboarding_welcome(pool: Any, user_id: UUID) -> bool:
    row = await pool.fetchrow(
        """
        UPDATE users
        SET onboarding_state='welcomed'
        WHERE id=$1 AND onboarding_state='pending'
        RETURNING id
        """,
        user_id,
    )
    return row is not None
