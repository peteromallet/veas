"""Shared check-in scheduling invariants."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


def require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("scheduled check-in datetime must be timezone-aware")
    return value.astimezone(UTC)


def _is_unique_violation(exc: Exception) -> bool:
    return (
        exc.__class__.__name__ == "UniqueViolationError"
        or getattr(exc, "sqlstate", None) == "23505"
        or getattr(exc, "pgcode", None) == "23505"
    )


async def _schedule_once(conn: Any, user_id: UUID, scheduled_for: datetime, context: dict[str, Any]) -> tuple[Any | None, Any]:
    old = await conn.fetchrow(
        """
        UPDATE scheduled_jobs
        SET status='superseded'
        WHERE user_id=$1 AND job_type='checkin' AND status='pending'
        RETURNING id
        """,
        user_id,
    )
    row = await conn.fetchrow(
        """
        INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status)
        VALUES ($1, 'checkin', $2, $3::jsonb, 'pending')
        RETURNING id AS job_id, scheduled_for
        """,
        user_id,
        scheduled_for,
        json.dumps(context),
    )
    return old, row


async def schedule_checkin_record(
    pool: Any,
    user_id: UUID,
    *,
    scheduled_for: datetime,
    context: dict[str, Any],
) -> tuple[Any | None, Any]:
    scheduled_for = require_aware_utc(scheduled_for)
    last_unique_violation: Exception | None = None
    for _attempt in range(2):
        try:
            async with pool.acquire() as conn:
                transaction = getattr(conn, "transaction", None)
                if transaction is None:
                    return await _schedule_once(conn, user_id, scheduled_for, context)
                async with transaction():
                    return await _schedule_once(conn, user_id, scheduled_for, context)
        except Exception as exc:
            if _is_unique_violation(exc):
                last_unique_violation = exc
                continue
            raise
    raise last_unique_violation or RuntimeError("failed to schedule check-in")
