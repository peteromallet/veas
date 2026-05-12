"""DB-backed global and per-(user,bot) system state helpers.

S2a NOTE: Per-(user,bot) pause WRITE-path is deferred to S2b.
The write-path for user_bot_state.paused is NOT implemented in this sprint.
Only the READ-path (user_bot_paused) is active, gating outbound sends,
scheduler dispatch, OOB releases, withheld-review emissions, and pacer
emissions. Global pause (system_state.is_paused) remains the kill switch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

USER_FACING_JOB_TYPES = (
    "weekly_summary",
    "checkin",
    "watch_item_due",
    "oob_review",
    "deferred_turn",
    "scheduled_task",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def is_paused(pool: Any) -> bool:
    paused_at = await pool.fetchval("SELECT paused_at FROM system_state WHERE key = 'global_pause'")
    return paused_at is not None


async def user_bot_paused(pool: Any, user_id: UUID, bot_id: str) -> bool:
    """Return True if per-(user, bot) pause is active.

    Reads user_bot_state.paused for the given user and bot.
    S2a: READ-path only. WRITE-path (setting paused=True) is deferred to S2b.
    """
    paused = await pool.fetchval(
        "SELECT paused FROM user_bot_state WHERE user_id = $1 AND bot_id = $2",
        user_id,
        bot_id,
    )
    return bool(paused)


async def pause(pool: Any, paused_by_user_id: UUID, *, now: datetime | None = None) -> None:
    now = now or _utc_now()
    await pool.execute(
        """
        INSERT INTO system_state (key, value, paused_at, paused_by_user_id, updated_at)
        VALUES ('global_pause', '{}'::jsonb, $1, $2, $1)
        ON CONFLICT (key) DO UPDATE
        SET paused_at = EXCLUDED.paused_at,
            paused_by_user_id = EXCLUDED.paused_by_user_id,
            updated_at = EXCLUDED.updated_at
        """,
        now,
        paused_by_user_id,
    )


async def resume(pool: Any, *, now: datetime | None = None) -> None:
    now = now or _utc_now()
    await pool.execute(
        """
        INSERT INTO system_state (key, value, paused_at, paused_by_user_id, updated_at)
        VALUES ('global_pause', '{}'::jsonb, NULL, NULL, $1)
        ON CONFLICT (key) DO UPDATE
        SET paused_at = NULL,
            paused_by_user_id = NULL,
            updated_at = EXCLUDED.updated_at
        """,
        now,
    )


async def supersede_pending_user_facing_jobs(pool: Any, *, now: datetime | None = None) -> None:
    now = now or _utc_now()
    await pool.execute(
        """
        UPDATE scheduled_jobs
        SET status = 'superseded',
            cancellation_reason = COALESCE(cancellation_reason, 'global pause'),
            claimed_at = NULL,
            claimed_by = NULL,
            updated_at = $1
        WHERE status = 'pending'
          AND job_type = ANY($2::text[])
        """,
        now,
        list(USER_FACING_JOB_TYPES),
    )
