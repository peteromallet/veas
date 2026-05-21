"""Recovery for raw messages and crashed turns."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.models.user import fetch_user_by_id
from app.services import inbound_queue, metrics, system_state
from app.services.coalescer_registry import CoalescerRegistry
from app.services.scope import scope_from_bot_turn_row, scope_from_message_row

logger = logging.getLogger(__name__)


def _coalescer_for_scope(registry: CoalescerRegistry, bot_id: str) -> Any | None:
    """Resolve the right bot coalescer through the registry.

    Returns ``None`` when no coalescer is registered for ``bot_id``; callers
    log a structured warning and leave the row in ``failed``.
    """
    return registry.get(bot_id)


async def _recovery_scopes(pool: Any) -> dict[str, set[UUID]]:
    """Build a map of bot_id → set(topic_id) for all inbound messages in
    recoverable states (raw/processing/failed).

    Used by the sweeper to iterate recovery helpers per (bot_id, topic_id).
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT bot_id, topic_id
        FROM messages
        WHERE direction = 'inbound'
          AND processing_state IN ('raw', 'processing', 'failed')
          AND bot_id IS NOT NULL
          AND topic_id IS NOT NULL
        """
    )
    result: dict[str, set[UUID]] = {}
    for row in rows:
        bid = row["bot_id"]
        tid = row["topic_id"]
        result.setdefault(bid, set()).add(tid)
    return result


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


async def _recover_legacy_invariants(pool: Any, *, now: datetime | None = None) -> None:
    """Always-on legacy invariants: scheduled_jobs reconciliation, bot_turn
    crash-marking (both pre-send and post-send), and retention-expiry sweeps
    for raw + (failed/processing) inbound rows.

    Never inspects ``failure_class``.  Runs regardless of the recovery-v2
    kill switch or coalescer readiness.
    """
    await recover_scheduled_jobs_on_startup(pool, now=now)

    # Mark crashed (pre-send) turns so the v2 half can pick them up for retry.
    # RETURNING keeps the fake-pool matcher's bookkeeping path; rows are
    # discarded here — v2 re-SELECTs the crashed set for dispatch.
    await pool.fetch(
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

    # Mark crashed-after-send turns terminal (no retry — outbound already left).
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

    settings = get_settings()
    retention_cutoff = (now or _utc_now()) - timedelta(days=settings.inbound_queue_retention_days)

    # Expire inbound raw rows past retention.
    await pool.execute(
        """
        UPDATE messages
        SET processing_state = 'expired',
            handling_result  = 'expired',
            handled_at       = now()
        WHERE direction = 'inbound'
          AND processing_state = 'raw'
          AND sent_at < $1
        """,
        retention_cutoff,
    )

    # Expire old failed/processing rows past retention.
    await pool.execute(
        """
        UPDATE messages
        SET processing_state = 'expired',
            handling_result  = 'expired',
            handled_at       = now()
        WHERE direction = 'inbound'
          AND processing_state IN ('failed', 'processing')
          AND sent_at < $1
        """,
        retention_cutoff,
    )

    # ── Live-prep orphan sweep ────────────────────────────────────────
    await sweep_orphaned_prepping(pool, now=now)


async def sweep_orphaned_prepping(
    pool: Any, *, now: datetime | None = None
) -> None:
    """Mark conversations stuck in ``prepping`` / ``preparing`` as ``prep_failed``.

    The background task's broad ``except`` is the primary defense against
    orphaned prepping sessions.  This sweep is the backstop — it catches
    sessions where the entire process (or event loop) died before the
    background task's except handler could fire.
    """
    settings = get_settings()
    timeout_minutes = settings.live_prep_orphan_timeout_minutes
    cutoff = (now or _utc_now()) - timedelta(minutes=timeout_minutes)
    result = await pool.execute(
        """
        UPDATE mediator.conversations
        SET status = 'prep_failed',
            session_fields = COALESCE(session_fields, '{}'::jsonb)
                             || jsonb_build_object('prep_error', 'orphaned')
        WHERE status IN ('prepping', 'preparing')
          AND created_at < $1
        """,
        cutoff,
    )
    # result is the command tag string e.g. "UPDATE 3"; extract count.
    count = 0
    try:
        count = int(str(result).split()[-1]) if result else 0
    except (ValueError, IndexError):
        pass
    if count:
        logger.info(
            "recovery: orphaned prepping sessions swept=%d "
            "timeout_minutes=%d",
            count,
            timeout_minutes,
        )


async def _recover_v2_inbound(
    pool: Any,
    registry: CoalescerRegistry,
    *,
    now: datetime | None = None,
) -> None:
    """Recovery-v2 inbound paths: requeue crashed turn-triggering messages,
    raw-message recovery, stale-processing recovery, retryable-failed recovery.

    Gated by the recovery_v2 kill switch (checked here and again at the top
    of each ``run_recovery_forever`` tick) AND ``registry.is_ready()``.  When
    a bot is unknown to the registry we log a structured warning and leave
    the row in ``failed``.
    """
    if await system_state.is_recovery_v2_killed(pool):
        logger.info("recovery-v2 skipped: kill switch engaged")
        return
    if not registry.is_ready():
        logger.info(
            "recovery-v2 skipped: registry not ready expected=%s installed=%s ready=%s",
            sorted(registry.expected),
            sorted(registry.installed.keys()),
            sorted(registry.ready),
        )
        return

    # ── Crashed-turn passive release ────────────────────────────────
    # Release any messages still owned by a crashed bot_turn back to
    # 'raw' so the raw-message branch below picks them up.  The turn is
    # marked terminal (completed_at + failure_reason) in the same atomic
    # CTE.  Pickup is delegated to the raw-message branch
    # (recovery.py:259-323); the 5-minute crash-detection latency is
    # enforced by the SELECT WHERE started_at < now() - interval '5 minutes'.
    #
    # Column semantics: clears messages.bot_turn_id (in-flight ownership)
    # but leaves messages.handled_by_turn_id untouched (historical handler).
    crashed = await pool.fetch(
        """
        SELECT id, triggering_message_ids, bot_id
        FROM bot_turns
        WHERE failure_reason = 'crashed'
          AND completed_at IS NULL
          AND final_output_message_id IS NULL
          AND started_at < now() - interval '5 minutes'
        """
    )
    _RECOVERY_LIFECYCLE_WRITER_SQL = (
        "SELECT set_config('app.lifecycle_writer', 'recovery', true)"
    )
    for row in crashed:
        turn_id = row["id"]
        message_ids = row["triggering_message_ids"]
        if not message_ids:
            continue
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(_RECOVERY_LIFECYCLE_WRITER_SQL)
                    result = await conn.fetchval(
                        """
                        WITH released AS (
                            UPDATE messages
                            SET processing_state      = 'raw',
                                bot_turn_id           = NULL,
                                processing_started_at = NULL
                            WHERE bot_turn_id = $1
                              AND processing_state IN ('processing', 'deferred')
                            RETURNING id
                        ),
                        turn_done AS (
                            UPDATE bot_turns
                            SET completed_at   = COALESCE(completed_at, now()),
                                failure_reason = COALESCE(failure_reason, 'crashed')
                            WHERE id = $1
                            RETURNING id
                        )
                        SELECT count(*) FROM released
                        """,
                        turn_id,
                    )
        except Exception:
            logger.exception(
                "recovery: crashed-turn release failed for bot_turn_id=%s",
                turn_id,
            )
            continue
        released = int(result or 0)
        if released:
            bot_id = row["bot_id"]
            logger.info(
                "recovery: released %d messages from crashed bot_turn_id=%s bot_id=%s",
                released,
                turn_id,
                bot_id,
            )
            metrics.incr(
                "recovery_released",
                value=released,
                bot=bot_id,
            )

    settings = get_settings()
    max_retries = settings.inbound_queue_max_retry_attempts

    raw_messages = await pool.fetch(
        """
        SELECT m.id, m.sender_id AS user_id, m.bot_id, m.topic_id
        FROM messages m
        WHERE m.direction = 'inbound'
          AND m.processing_state = 'raw'
          AND m.sent_at < now() - interval '30 seconds'
          AND (m.next_retry_at IS NULL OR m.next_retry_at <= now())
          AND (m.failure_class IS NULL
               OR m.failure_class NOT IN ('terminal_post_send', 'infra_bug'))
          AND (
            NOT EXISTS (
              SELECT 1
              FROM bot_turns bt
              WHERE bt.triggering_message_ids @> ARRAY[m.id]
            )
            OR (
              m.processing_attempts < $1
              AND (
                m.handling_result = 'failed'
                OR EXISTS (
                  SELECT 1
                  FROM bot_turns bt
                  WHERE bt.triggering_message_ids @> ARRAY[m.id]
                    AND bt.failure_reason IS NOT NULL
                    AND bt.final_output_message_id IS NULL
                    AND m.processing_attempts > 0
                )
              )
            )
          )
        """,
        max_retries,
    )
    for row in raw_messages:
        try:
            scope = scope_from_message_row(row)
        except ValueError as exc:
            logger.warning(
                "skipping raw message recovery for message_id=%s: %s",
                _row_get(row, "id"),
                exc,
            )
            continue
        user = await fetch_user_by_id(pool, scope.user_id)
        if user is None:
            logger.warning(
                "skipping raw message recovery for message_id=%s: missing user_id=%s",
                row["id"],
                scope.user_id,
            )
            continue
        target_coalescer = _coalescer_for_scope(registry, scope.bot_id)
        if target_coalescer is None:
            logger.warning(
                "recovery-v2: no coalescer for bot_id=%s; leaving message_id=%s in failed",
                scope.bot_id,
                row["id"],
            )
            metrics.incr(
                "recovery_skipped_missing_coalescer",
                bot=scope.bot_id,
            )
            continue
        await target_coalescer.add(user.id, row["id"], user, source="recovery", scope=scope)  # pause-check via send_outbound
        metrics.incr(
            "recovery_requeued",
            bot=scope.bot_id,
            reason="raw_message",
        )

    # Recover stale inbound ``processing`` rows.
    stale_processing_recovered = 0
    scope_map = await _recovery_scopes(pool)
    for bot_id, topic_ids in scope_map.items():
        for topic_id in topic_ids:
            count = await inbound_queue.recover_stale_processing(
                pool,
                bot_id=bot_id,
                topic_id=topic_id,
                stale_seconds=300,
                limit=50,
            )
            if count:
                stale_processing_recovered += count
                logger.info(
                    "recovery: stale processing recovered=%d bot_id=%s topic_id=%s",
                    count,
                    bot_id,
                    str(topic_id),
                )
                metrics.incr(
                    "recovery_requeued",
                    value=count,
                    bot=bot_id,
                    reason="stale_processing",
                )
    if stale_processing_recovered:
        logger.info("recovery: total stale processing recovered=%d", stale_processing_recovered)

    # Recover retryable inbound ``failed`` rows.  ``recover_retryable_failed``
    # only flips state→raw; ``claim_messages_for_turn`` will NULL the
    # lifecycle columns via the writer-marker mutator path.
    failed_recovered = 0
    failed_scope_map = await _recovery_scopes(pool)
    for bot_id, topic_ids in failed_scope_map.items():
        for topic_id in topic_ids:
            count = await inbound_queue.recover_retryable_failed(
                pool,
                bot_id=bot_id,
                topic_id=topic_id,
                max_retries=max_retries,
                limit=50,
            )
            if count:
                failed_recovered += count
                logger.info(
                    "recovery: retryable failed recovered=%d bot_id=%s topic_id=%s",
                    count,
                    bot_id,
                    str(topic_id),
                )
                metrics.incr(
                    "recovery_requeued",
                    value=count,
                    bot=bot_id,
                    reason="retryable_failed",
                )
    if failed_recovered:
        logger.info("recovery: total retryable failed recovered=%d", failed_recovered)


async def recover_on_startup(
    pool: Any,
    registry: CoalescerRegistry,
    *,
    now: datetime | None = None,
) -> None:
    """Top-level recovery entry point.

    Always runs the legacy invariants (scheduled_jobs reconciliation,
    bot_turn crash-marking, retention-expiry sweeps).  The v2 inbound half
    runs only when the kill switch is NOT set and the coalescer registry is
    ready.
    """
    await _recover_legacy_invariants(pool, now=now)
    if await system_state.is_paused(pool):
        return
    if await system_state.is_recovery_v2_killed(pool):
        logger.info("recovery-v2 skipped: kill switch engaged")
        return
    await _recover_v2_inbound(pool, registry, now=now)


async def run_recovery_forever(
    pool: Any,
    registry: CoalescerRegistry,
    *,
    interval_seconds: float = 30.0,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            # Legacy invariants always run; the kill switch only gates the v2
            # inbound half.  Check it at the top of each tick so an operator
            # flipping the switch takes effect on the next iteration.
            await _recover_legacy_invariants(pool)
            if await system_state.is_paused(pool):
                continue
            if await system_state.is_recovery_v2_killed(pool):
                logger.info("recovery-v2 skipped: kill switch engaged")
                continue
            await _recover_v2_inbound(pool, registry)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("recovery loop tick failed")
