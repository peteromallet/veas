"""Recovery for raw messages and crashed turns."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.models.user import fetch_user_by_id
from app.services import system_state
from app.services.scope import scope_from_bot_turn_row, scope_from_message_row

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        return getattr(row, key, default)


async def recover_scheduled_jobs_on_startup(pool: Any, *, now: datetime | None = None) -> None:
    now = now or _utc_now()
    await pool.execute(
        """
        UPDATE scheduled_jobs
        SET status = 'cancelled',
            cancellation_reason = 'too stale',
            claimed_at = NULL,
            claimed_by = NULL,
            updated_at = $1
        WHERE status = 'pending'
          AND scheduled_for < $1::timestamptz - interval '24 hours'
        """,
        now,
    )
    await pool.execute(
        """
        UPDATE scheduled_jobs
        SET context = jsonb_set(COALESCE(context, '{}'::jsonb), '{delayed}', 'true'::jsonb, true),
            delayed = true,
            claimed_at = NULL,
            claimed_by = NULL,
            updated_at = $1
        WHERE status = 'pending'
          AND scheduled_for < $1::timestamptz - interval '1 hour'
          AND scheduled_for >= $1::timestamptz - interval '24 hours'
        """,
        now,
    )
    await pool.execute(
        """
        UPDATE scheduled_jobs
        SET claimed_at = NULL,
            claimed_by = NULL,
            updated_at = $1
        WHERE status = 'pending'
          AND scheduled_for < $1
          AND scheduled_for >= $1::timestamptz - interval '1 hour'
        """,
        now,
    )


async def recover_on_startup(pool: Any, coalescer: Any, *, now: datetime | None = None) -> None:
    await recover_scheduled_jobs_on_startup(pool, now=now)
    if await system_state.is_paused(pool):
        return
    crashed = await pool.fetch(
        """
        UPDATE bot_turns
        SET failure_reason='crashed'
        WHERE completed_at IS NULL
          AND final_output_message_id IS NULL
          AND failure_reason IS NULL
          AND started_at < now() - interval '5 minutes'
        RETURNING id, triggering_message_ids, user_in_context AS user_id, bot_id, topic_id
        """
    )
    for row in crashed:
        message_ids = row["triggering_message_ids"]
        if not message_ids:
            continue
        try:
            scope = scope_from_bot_turn_row(row)
        except ValueError as exc:
            logger.warning("skipping crashed turn recovery for bot_turn_id=%s: %s", _row_get(row, "id"), exc)
            continue
        user = await fetch_user_by_id(pool, scope.user_id)
        if user is None:
            logger.warning(
                "skipping crashed turn recovery for bot_turn_id=%s: missing user_id=%s",
                _row_get(row, "id"),
                scope.user_id,
            )
            continue
        await coalescer.add_burst(user.id, message_ids, user, scope=scope)  # pause-check via send_outbound

    await pool.execute(
        """
        UPDATE bot_turns
        SET failure_reason='crashed_after_send'
        WHERE completed_at IS NULL
          AND final_output_message_id IS NOT NULL
          AND failure_reason IS NULL
          AND started_at < now() - interval '5 minutes'
        """
    )

    raw_messages = await pool.fetch(
        """
        SELECT m.id, m.sender_id AS user_id, m.bot_id, m.topic_id
        FROM messages m
        WHERE m.processing_state='raw'
          AND m.sent_at < now() - interval '30 seconds'
          AND NOT EXISTS (
              SELECT 1
              FROM bot_turns bt
              WHERE bt.triggering_message_ids @> ARRAY[m.id]
          )
        """
    )
    for row in raw_messages:
        try:
            scope = scope_from_message_row(row)
        except ValueError as exc:
            logger.warning("skipping raw message recovery for message_id=%s: %s", _row_get(row, "id"), exc)
            continue
        user = await fetch_user_by_id(pool, scope.user_id)
        if user is None:
            logger.warning("skipping raw message recovery for message_id=%s: missing user_id=%s", row["id"], scope.user_id)
            continue
        await coalescer.add(user.id, row["id"], user, source="recovery", scope=scope)  # pause-check via send_outbound


async def run_recovery_forever(pool: Any, coalescer: Any, *, interval_seconds: float = 30.0) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await recover_on_startup(pool, coalescer)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("recovery loop tick failed")
