"""
Provider-agnostic inbound queue transition helpers.

Queue states and legal transitions
-----------------------------------
States (as enforced by the messages_processing_state_check constraint):

  raw         Stored, not yet claimed by any worker.
  deferred    Intentionally waiting for coalescing or pacing.
  processing  Claimed by a worker/turn; actively being handled.
  processed   Successfully handled by a completed turn.
  expired     Intentionally no longer needs direct handling.
  failed      Attempted and failed; retryable or inspectable.
  withheld    Legacy state retained for existing rows (no new rows enter this).

Legal transitions:

  raw ──────────────────> processing   (claim_messages_for_turn)
  raw ──────────────────> deferred     (defer_messages)
  raw ──────────────────> expired      (expire_messages — past retention)
  deferred ─────────────> processing   (claim_messages_for_turn)
  processing ───────────> processed    (complete_messages)
  processing ───────────> failed       (fail_messages)
  processing ───────────> raw          (recover_stale_processing)
  failed ───────────────> raw          (recover_retryable_failed)
  failed ───────────────> expired      (past retention or retries exhausted,
                                        handled separately by sweeper expiry logic)

Terminal states: processed, expired, withheld.
Once a row reaches a terminal state it MUST NOT be retried or re-enqueued.

All helpers in this module include ``direction='inbound'`` guards.
Outbound rows are never touched by these functions.

Lifecycle-column writer invariant (recovery-v2)
-----------------------------------------------
``claim_messages_for_turn``, ``complete_messages``, and ``fail_messages`` are
the SOLE legitimate mutators of ``messages.next_retry_at`` and
``messages.failure_class``.  Migration 0042 installs a BEFORE UPDATE row
trigger (mediator.assert_lifecycle_columns_writer) that RAISEs unless the
mutating transaction has set the txn-local GUC
``app.lifecycle_writer = 'inbound_queue'`` via
``set_config('app.lifecycle_writer', 'inbound_queue', true)`` on the same
connection.  Any ad-hoc UPDATE elsewhere that touches those columns will
fail loudly at write time.

Design decisions
----------------
See SD-001 through SD-007 in the durable-inbound-queue-hardening brief and
SD-A1-T1 through SD-A1-T8 in the agent-reliability cleanup brief.

- claim_messages_for_turn uses an atomic CTE (UPDATE ... WHERE ... RETURNING)
  to prevent two workers from claiming the same row.
- handled_by_turn_id serves double duty: set during claim (active processing
  turn) and during completion (terminal handled-by metadata).  The sweeper
  MUST check processing_state, not just handled_by_turn_id, to decide whether
  a row needs recovery (see DEBT-095).
- Silencing / reaction paths that never open a bot_turns row call
  complete_messages with handled_by_turn_id=None (see DEBT-097).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.services import metrics

logger = logging.getLogger(__name__)


_SET_LIFECYCLE_WRITER_SQL = (
    "SELECT set_config('app.lifecycle_writer', 'inbound_queue', true)"
)


# ── ledger dual-write (Project C, C2) ────────────────────────────────────────
#
# When ``settings.ledger_dual_write_enabled`` is True, the claim / complete /
# fail helpers ALSO write rows to ``mediator.inbound_handling_attempts``.
# The read path (recovery / retry sweepers) is unchanged — it still reads
# ``messages.next_retry_at`` + ``messages.failure_class``.  The flag is the
# kill switch: flip OFF to revert to messages-only writes without a redeploy.
#
# Failure isolation: the ledger write happens on the SAME connection inside
# the SAME transaction as the messages UPDATE.  If the ledger insert raises
# (table missing on stale environments, unique-index violation, etc.) the
# whole transaction rolls back, leaving messages unchanged.  That's the safe
# behaviour: better to fail loudly than to silently drift the two stores.


def _ledger_dual_write_enabled() -> bool:
    """Read the ``ledger_dual_write_enabled`` flag without imposing a
    Settings load on call sites that didn't already need one.

    The pre-C1 fast paths (``claim_messages_for_turn`` /
    ``complete_messages``) don't otherwise touch ``get_settings()``, and
    forcing one would break tests that exercise those helpers without a
    full app env.  This wrapper returns False on any settings-load
    failure, which is the same default as a successful load with the
    flag unset.
    """
    try:
        settings = get_settings()
    except Exception:
        return False
    return bool(getattr(settings, "ledger_dual_write_enabled", False))


async def _ledger_open_active(
    conn: Any,
    *,
    message_id: UUID,
    bot_id: str,
    topic_id: UUID,
    attempt_number: int,
    created_by: str,
    bot_turn_id: UUID | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO inbound_handling_attempts
            (message_id, bot_turn_id, bot_id, topic_id, attempt_number,
             status, created_by, started_at)
        VALUES ($1, $2, $3, $4, $5, 'active', $6, now())
        """,
        message_id,
        bot_turn_id,
        bot_id,
        topic_id,
        attempt_number,
        created_by,
    )


async def _ledger_mark_succeeded(
    conn: Any,
    *,
    message_id: UUID,
    bot_turn_id: UUID | None,
) -> None:
    await conn.execute(
        """
        UPDATE inbound_handling_attempts
        SET status        = 'succeeded',
            completed_at  = now(),
            bot_turn_id   = COALESCE($2, bot_turn_id),
            failure_class = NULL,
            failure_reason = NULL,
            next_retry_at = NULL
        WHERE message_id = $1
          AND status = 'active'
        """,
        message_id,
        bot_turn_id,
    )


async def _ledger_mark_failed(
    conn: Any,
    *,
    message_id: UUID,
    failure_class: str,
    failure_reason: str | None,
    bot_turn_id: UUID | None,
) -> None:
    await conn.execute(
        """
        UPDATE inbound_handling_attempts
        SET status         = 'failed',
            completed_at   = now(),
            failure_class  = $2,
            failure_reason = $3,
            bot_turn_id    = COALESCE($4, bot_turn_id),
            next_retry_at  = (
                SELECT m.next_retry_at FROM messages m WHERE m.id = $1
            )
        WHERE message_id = $1
          AND status = 'active'
        """,
        message_id,
        failure_class,
        failure_reason,
        bot_turn_id,
    )




# ── failure-class taxonomy (recovery-v2, SD-A1-T5/T7) ────────────────────────

# The three durable failure classes recorded on messages.failure_class.
FAILURE_CLASSES: frozenset[str] = frozenset({
    "retryable_pre_send",
    "terminal_post_send",
    "infra_bug",
})

# Map from a turn's failure_reason string to a durable failure_class.
# Live mapping covers reasons emitted by the current code paths
# (provider_send_failed, llm_timeout, tool_validation_recoverable_exhausted,
# crashed, transcription_failed, vision_failed).  Forward-compat entries
# (spend_cap, newer_inbound_before_final_send, crashed_after_send) are
# included for future use; do NOT fabricate test call sites for them.
# Anything not present here (including ``"unknown"``) maps to ``infra_bug``
# via dict.get(reason, "infra_bug") at the call site.
FAILURE_REASON_TO_CLASS: dict[str, str] = {
    # ── live (currently emitted by inbound/turn machinery) ──────────────
    "provider_send_failed": "retryable_pre_send",
    "llm_timeout": "retryable_pre_send",
    # Catch-all when the LLM phase fails without a more specific reason
    # (typically: provider chain exhausted with non-retryable upstream
    # errors). Distinct from `llm_timeout` which is reserved for actual
    # clock timeouts on the provider call.
    "llm_phase_failed": "retryable_pre_send",
    "tool_validation_recoverable_exhausted": "retryable_pre_send",
    "crashed": "retryable_pre_send",
    "transcription_failed": "retryable_pre_send",
    "vision_failed": "retryable_pre_send",
    # ── Project A2 provider-chain failure reasons ──────────────────────
    "provider_fallback_killed": "retryable_pre_send",
    "same_provider_fallback_noop": "retryable_pre_send",
    "fallback_breaker_open": "retryable_pre_send",
    "respond_cap_no_output": "retryable_pre_send",
    # Anthropic→DeepSeek is a genuine configuration bug, not user-recoverable.
    "unsupported_chain_anthropic_to_deepseek": "infra_bug",
    # ── forward-compat (dead today; do NOT fabricate test call sites) ──
    "spend_cap": "retryable_pre_send",
    "newer_inbound_before_final_send": "retryable_pre_send",
    "crashed_after_send": "terminal_post_send",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ── claim ────────────────────────────────────────────────────────────────────

async def _claim_messages_for_turn_in_tx(
    conn: Any,
    message_ids: list[UUID],
    *,
    bot_id: str,
    topic_id: UUID,
    new_bot_turn_id: UUID | None = None,
) -> list[UUID]:
    """Claim eligible inbound messages inside an already-open transaction.

    Caller MUST already hold a transaction on ``conn`` AND have executed
    ``_SET_LIFECYCLE_WRITER_SQL`` on the same connection.

    ``new_bot_turn_id`` is the in-flight ownership pointer.  When None
    (legacy caller), existing ``bot_turn_id`` is preserved (backward compat).
    When set, the claim CTE stamps matching rows with this turn id AND
    refuses to claim rows that are already claimed by a DIFFERENT in-flight
    turn (the WHERE clause allows NULL bot_turn_id, NULL new_bot_turn_id,
    or matching bot_turn_id).
    """
    if not message_ids:
        return []

    rows = await conn.fetch(
        """
        WITH claimed AS (
            UPDATE messages
            SET processing_state      = 'processing',
                processing_started_at = now(),
                processing_attempts   = processing_attempts + 1,
                processing_error      = NULL,
                next_retry_at         = NULL,
                bot_turn_id           = COALESCE($4::uuid, bot_turn_id)
            WHERE id = ANY($1::uuid[])
              AND direction = 'inbound'
              AND processing_state IN ('raw', 'deferred')
              AND (processing_started_at IS NULL
                   OR processing_started_at < now() - interval '5 minutes')
              AND bot_id = $2
              AND topic_id = $3
              AND (bot_turn_id IS NULL OR $4::uuid IS NULL OR bot_turn_id = $4::uuid)
            RETURNING id
        )
        SELECT id FROM claimed
        """,
        message_ids,
        bot_id,
        topic_id,
        new_bot_turn_id,
    )
    if rows and _ledger_dual_write_enabled():
        attempt_rows = await conn.fetch(
            "SELECT id, processing_attempts FROM messages "
            "WHERE id = ANY($1::uuid[])",
            [row["id"] for row in rows],
        )
        attempts_by_id = {
            r["id"]: int(r["processing_attempts"] or 1)
            for r in attempt_rows
        }
        for row in rows:
            await _ledger_open_active(
                conn,
                message_id=row["id"],
                bot_id=bot_id,
                topic_id=topic_id,
                attempt_number=attempts_by_id.get(row["id"], 1),
                created_by="live",
                bot_turn_id=new_bot_turn_id,
            )
    return [row["id"] for row in rows]


async def claim_messages_for_turn(
    pool: Any,
    message_ids: list[UUID],
    *,
    bot_id: str,
    topic_id: UUID,
    new_bot_turn_id: UUID | None = None,
) -> list[UUID]:
    """Atomically claim eligible inbound messages for a turn.

    Only rows matching ALL of these conditions are claimed:
    - ``direction = 'inbound'``
    - ``processing_state IN ('raw', 'deferred')``
    - ``bot_id`` and ``topic_id`` match the caller's scope
    - ``processing_started_at IS NULL`` or older than 5 minutes (stale claim)
    - ``bot_turn_id IS NULL`` or matches ``new_bot_turn_id`` (in-flight owner)

    Claimed rows are immediately:
    - Set to ``processing_state = 'processing'``
    - Stamped with ``processing_started_at = now()``
    - Have ``processing_attempts`` incremented
    - Have ``processing_error`` cleared
    - Have ``bot_turn_id`` set to ``new_bot_turn_id`` when provided

    Returns only the ids that were *actually* claimed (race-resistant:
    rows that did not match the WHERE at COMMIT time are silently excluded).
    """
    if not message_ids:
        return []

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_SET_LIFECYCLE_WRITER_SQL)
            claimed = await _claim_messages_for_turn_in_tx(
                conn,
                message_ids,
                bot_id=bot_id,
                topic_id=topic_id,
                new_bot_turn_id=new_bot_turn_id,
            )
    unclaimed = len(message_ids) - len(claimed)
    if unclaimed:
        logger.debug(
            "claim_messages_for_turn: claimed=%d unclaimed=%d bot_id=%s topic_id=%s",
            len(claimed),
            unclaimed,
            bot_id,
            str(topic_id),
        )
    # A3 work item 6: emit one inbound_attempts_started increment per claimed
    # row so the started/completed ratio reflects attempt counts, not batches.
    if claimed:
        metrics.incr(
            "inbound_attempts_started",
            value=len(claimed),
            bot=bot_id,
        )
    return claimed


# ── terminal completion ──────────────────────────────────────────────────────

async def _complete_messages_in_tx(
    conn: Any,
    message_ids: list[UUID],
    *,
    handling_result: str,
    handled_by_turn_id: UUID | None = None,
    bot_id: str,
    topic_id: UUID,
) -> int:
    """Mark inbound messages as successfully handled (tx already held).

    Caller MUST already hold a transaction on ``conn`` AND have executed
    ``_SET_LIFECYCLE_WRITER_SQL`` on the same connection.

    Clears ``bot_turn_id`` (in-flight owner) while preserving
    ``handled_by_turn_id`` (historical terminal handler).
    """
    if not message_ids:
        return 0

    result = await conn.execute(
        """
        UPDATE messages
        SET processing_state   = 'processed',
            handling_result    = $4,
            handled_at         = now(),
            handled_by_turn_id = $5,
            bot_turn_id        = NULL,
            next_retry_at      = NULL,
            failure_class      = NULL
        WHERE id = ANY($1::uuid[])
          AND direction = 'inbound'
          AND bot_id = $2
          AND topic_id = $3
        """,
        message_ids,
        bot_id,
        topic_id,
        handling_result,
        handled_by_turn_id,
    )
    if _ledger_dual_write_enabled():
        for mid in message_ids:
            await _ledger_mark_succeeded(
                conn,
                message_id=mid,
                bot_turn_id=handled_by_turn_id,
            )
    return _parse_update_count(result)


async def complete_messages(
    pool: Any,
    message_ids: list[UUID],
    *,
    handling_result: str,
    handled_by_turn_id: UUID | None = None,
    bot_id: str,
    topic_id: UUID,
) -> int:
    """Mark inbound messages as successfully handled.

    Parameters
    ----------
    handling_result : str
        One of 'replied', 'silent', 'withheld_newer_inbound', 'no_action'.
    handled_by_turn_id : UUID | None
        The bot_turns row that handled these messages.  May be ``None`` for
        pacer/debouncer silence/react paths that never open a turn.
    """
    if not message_ids:
        return 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_SET_LIFECYCLE_WRITER_SQL)
            updated = await _complete_messages_in_tx(
                conn,
                message_ids,
                handling_result=handling_result,
                handled_by_turn_id=handled_by_turn_id,
                bot_id=bot_id,
                topic_id=topic_id,
            )
    if updated:
        logger.debug(
            "complete_messages: updated=%d result=%s bot_id=%s topic_id=%s",
            updated,
            handling_result,
            bot_id,
            str(topic_id),
        )
        # A3 work item 6: completion is a \"success\" terminal class.  SD-002
        # only defines 3 failure classes (retryable_pre_send / terminal_post_send /
        # infra_bug); ``success`` is the sentinel label used here to keep the
        # started/completed ratio interpretable.
        metrics.incr(
            "inbound_attempts_completed",
            value=updated,
            bot=bot_id,
            failure_class="success",
        )
    return updated


# ── failure ──────────────────────────────────────────────────────────────────

async def _fail_messages_in_tx(
    conn: Any,
    message_ids: list[UUID],
    *,
    processing_error: str,
    handled_by_turn_id: UUID | None = None,
    bot_id: str,
    topic_id: UUID,
    failure_class: str,
    failure_reason: str | None,
) -> int:
    """Mark inbound messages as failed (tx already held).

    Caller MUST already hold a transaction on ``conn`` AND have executed
    ``_SET_LIFECYCLE_WRITER_SQL`` on the same connection.

    Clears ``bot_turn_id`` (in-flight owner) while preserving
    ``handled_by_turn_id`` (historical terminal handler).

    ``next_retry_at`` is computed in-SQL as an exponential backoff for
    ``retryable_pre_send`` rows and NULL for terminal classes.
    The caller must provide ``backoff_base_seconds`` and ``backoff_cap_seconds``
    (pre-fetched from settings).
    """
    if failure_class not in FAILURE_CLASSES:
        raise ValueError(
            f"unknown failure_class: {failure_class!r}; "
            f"expected one of {sorted(FAILURE_CLASSES)}"
        )
    if not message_ids:
        return 0

    settings = get_settings()
    backoff_base_seconds = settings.recovery_v2_retry_base_seconds
    backoff_cap_seconds = settings.recovery_v2_retry_cap_seconds
    dual_write = bool(getattr(settings, "ledger_dual_write_enabled", False))

    result = await conn.execute(
        """
        UPDATE messages
        SET processing_state   = 'failed',
            processing_error   = $4,
            handling_result    = 'failed',
            handled_by_turn_id = COALESCE($5, handled_by_turn_id),
            bot_turn_id        = NULL,
            handled_at         = CASE WHEN $5 IS NOT NULL THEN now()
                                      ELSE handled_at END,
            failure_class      = $6,
            next_retry_at      = CASE
                WHEN $6 = 'retryable_pre_send'
                THEN now() + (
                    LEAST(
                        $7::numeric * power(2, GREATEST(processing_attempts - 1, 0)),
                        $8::numeric
                    ) || ' seconds'
                )::interval
                ELSE NULL
            END
        WHERE id = ANY($1::uuid[])
          AND direction = 'inbound'
          AND bot_id = $2
          AND topic_id = $3
        """,
        message_ids,
        bot_id,
        topic_id,
        processing_error,
        handled_by_turn_id,
        failure_class,
        backoff_base_seconds,
        backoff_cap_seconds,
    )
    if dual_write:
        for mid in message_ids:
            await _ledger_mark_failed(
                conn,
                message_id=mid,
                failure_class=failure_class,
                failure_reason=failure_reason,
                bot_turn_id=handled_by_turn_id,
            )
    return _parse_update_count(result)


async def fail_messages(
    pool: Any,
    message_ids: list[UUID],
    *,
    processing_error: str,
    handled_by_turn_id: UUID | None = None,
    bot_id: str,
    topic_id: UUID,
    failure_class: str,
    failure_reason: str | None,
) -> int:
    """Mark inbound messages as failed with an error description.

    Sets ``handling_result = 'failed'`` and stamps ``handled_at`` only when
    a turn_id is provided (i.e. the turn existed and completed enough to
    record the failure).  Otherwise the row stays in 'failed' for future
    sweeper recovery.

    ``failure_class`` is required and must be one of :data:`FAILURE_CLASSES`.
    ``next_retry_at`` is computed in-SQL as an exponential backoff for
    ``retryable_pre_send`` rows (base/cap from
    ``recovery_v2_retry_base_seconds`` / ``recovery_v2_retry_cap_seconds``)
    and NULL for terminal classes.  ``failure_reason`` is currently passed
    through for caller context only; the durable class is what the row
    trigger and retry sweeper read.
    """
    if failure_class not in FAILURE_CLASSES:
        raise ValueError(
            f"unknown failure_class: {failure_class!r}; "
            f"expected one of {sorted(FAILURE_CLASSES)}"
        )
    if not message_ids:
        return 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_SET_LIFECYCLE_WRITER_SQL)
            updated = await _fail_messages_in_tx(
                conn,
                message_ids,
                processing_error=processing_error,
                handled_by_turn_id=handled_by_turn_id,
                bot_id=bot_id,
                topic_id=topic_id,
                failure_class=failure_class,
                failure_reason=failure_reason,
            )
    if updated:
        logger.debug(
            "fail_messages: updated=%d error=%s bot_id=%s topic_id=%s "
            "failure_class=%s failure_reason=%s",
            updated,
            processing_error[:120],
            bot_id,
            str(topic_id),
            failure_class,
            failure_reason,
        )
        # A3 work item 6: per-failure-class attempt-completed counter.
        # ``failure_class`` is one of the SD-002 three.
        metrics.incr(
            "inbound_attempts_completed",
            value=updated,
            bot=bot_id,
            failure_class=failure_class,
        )
    return updated


# ── defer ────────────────────────────────────────────────────────────────────

async def defer_messages(
    pool: Any,
    message_ids: list[UUID],
    *,
    bot_id: str,
    topic_id: UUID,
) -> int:
    """Move inbound messages to ``deferred`` state (e.g. for spend-cap deferral).

    Rows in 'deferred' are eligible for later re-claim by claim_messages_for_turn.
    """
    if not message_ids:
        return 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_SET_LIFECYCLE_WRITER_SQL)
            result = await conn.execute(
                """
                UPDATE messages
                SET processing_state = 'deferred'
                WHERE id = ANY($1::uuid[])
                  AND direction = 'inbound'
                  AND bot_id = $2
                  AND topic_id = $3
                """,
                message_ids,
                bot_id,
                topic_id,
            )
    updated = _parse_update_count(result)
    if updated:
        logger.debug(
            "defer_messages: updated=%d bot_id=%s topic_id=%s",
            updated,
            bot_id,
            str(topic_id),
        )
    return updated


# ── expire ───────────────────────────────────────────────────────────────────

async def expire_messages(
    pool: Any,
    message_ids: list[UUID],
    *,
    bot_id: str,
    topic_id: UUID,
) -> int:
    """Mark inbound messages as ``expired`` (outside retention, no longer needed).

    Terminal state — rows are not eligible for recovery or retry.
    """
    if not message_ids:
        return 0

    result = await pool.execute(
        """
        UPDATE messages
        SET processing_state = 'expired',
            handling_result  = 'expired',
            handled_at       = now()
        WHERE id = ANY($1::uuid[])
          AND direction = 'inbound'
          AND bot_id = $2
          AND topic_id = $3
        """,
        message_ids,
        bot_id,
        topic_id,
    )
    updated = _parse_update_count(result)
    if updated:
        logger.debug(
            "expire_messages: updated=%d bot_id=%s topic_id=%s",
            updated,
            bot_id,
            str(topic_id),
        )
    return updated


# ── recovery ─────────────────────────────────────────────────────────────────

async def recover_stale_processing(
    pool: Any,
    *,
    bot_id: str,
    topic_id: UUID,
    stale_seconds: int = 300,
    limit: int = 50,
) -> int:
    """Recover inbound messages stuck in ``processing`` state.

    Rows with ``processing_started_at`` older than *stale_seconds* are reset
    to ``raw`` so the sweeper or coalescer can re-process them.  This handles
    worker crashes that leave rows in ``processing`` indefinitely.
    """
    result = await pool.execute(
        """
        UPDATE messages
        SET processing_state      = 'raw',
            processing_started_at = NULL
        WHERE id IN (
            SELECT id FROM messages
            WHERE direction = 'inbound'
              AND processing_state = 'processing'
              AND processing_started_at < now() - $3::interval
              AND bot_id = $1
              AND topic_id = $2
            LIMIT $4
        )
        """,
        bot_id,
        topic_id,
        timedelta(seconds=stale_seconds),
        limit,
    )
    updated = _parse_update_count(result)
    if updated:
        logger.info(
            "recover_stale_processing: recovered=%d bot_id=%s topic_id=%s "
            "stale_seconds=%d",
            updated,
            bot_id,
            str(topic_id),
            stale_seconds,
        )
    return updated


async def recover_retryable_failed(
    pool: Any,
    *,
    bot_id: str,
    topic_id: UUID,
    max_retries: int = 3,
    limit: int = 50,
) -> int:
    """Recover inbound messages in ``failed`` state that are still retryable.

    Only rows with ``processing_attempts < max_retries`` are reset to ``raw``.
    Rows that have exceeded the retry cap are left in ``failed`` for manual
    inspection (they are terminal for automatic recovery).
    """
    # Recovery-v2 ordering: this UPDATE flips ``failed`` rows back to ``raw``
    # but does NOT clear ``next_retry_at`` / ``failure_class`` — the subsequent
    # ``claim_messages_for_turn`` is the sole mutator that NULLs them via the
    # writer-marker path. The trigger is column-scoped so this flip-to-raw is
    # harmless.
    result = await pool.execute(
        """
        UPDATE messages
        SET processing_state      = 'raw',
            processing_started_at = NULL
        WHERE id IN (
            SELECT id FROM messages
            WHERE direction = 'inbound'
              AND processing_state = 'failed'
              AND processing_attempts < $3
              AND bot_id = $1
              AND topic_id = $2
              AND (next_retry_at IS NULL OR next_retry_at <= now())
              AND (failure_class IS NULL
                   OR failure_class NOT IN ('terminal_post_send', 'infra_bug'))
            LIMIT $4
        )
        """,
        bot_id,
        topic_id,
        max_retries,
        limit,
    )
    updated = _parse_update_count(result)
    if updated:
        logger.info(
            "recover_retryable_failed: recovered=%d bot_id=%s topic_id=%s "
            "max_retries=%d",
            updated,
            bot_id,
            str(topic_id),
            max_retries,
        )
    return updated


# ── ledger reconciliation (Project C, C2) ────────────────────────────────────


async def reconcile_ledger_active_attempts(pool: Any) -> int:
    """Synthesise missing 'active' ledger rows for in-flight messages.

    Walks rows in ``messages`` where ``processing_state = 'processing'``
    that have no ``active`` entry in ``inbound_handling_attempts`` and
    inserts one (``created_by='catch_up'``).  Idempotent: existing active
    rows are not touched; the partial unique index would refuse a
    duplicate anyway.

    Returns the number of attempts opened.  No-op when
    ``ledger_dual_write_enabled`` is False — the flag is the kill switch.
    """
    settings = get_settings()
    if not getattr(settings, "ledger_dual_write_enabled", False):
        return 0

    inserted = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT m.id, m.bot_id, m.topic_id,
                       m.processing_attempts, m.handled_by_turn_id
                FROM messages m
                WHERE m.direction = 'inbound'
                  AND m.processing_state = 'processing'
                  AND NOT EXISTS (
                      SELECT 1 FROM inbound_handling_attempts a
                      WHERE a.message_id = m.id AND a.status = 'active'
                  )
                """
            )
            for row in rows:
                try:
                    await _ledger_open_active(
                        conn,
                        message_id=row["id"],
                        bot_id=row["bot_id"] or "unknown",
                        topic_id=row["topic_id"],
                        attempt_number=int(row["processing_attempts"] or 1),
                        created_by="catch_up",
                        bot_turn_id=row.get("handled_by_turn_id"),
                    )
                    inserted += 1
                except Exception:
                    logger.exception(
                        "reconcile_ledger_active_attempts: insert failed "
                        "message_id=%s",
                        row["id"],
                    )
    if inserted:
        logger.info(
            "reconcile_ledger_active_attempts: inserted=%d catch_up rows", inserted
        )
        metrics.incr(
            "ledger_reconcile_catch_up_opened",
            value=inserted,
        )
    return inserted


# ── internal helpers ─────────────────────────────────────────────────────────

def _parse_update_count(result: Any) -> int:
    """Parse the row count from an asyncpg ``execute`` result string.

    asyncpg returns a string like ``"UPDATE 5"`` for successful UPDATEs.
    Returns 0 for unrecognised formats or None.
    """
    if isinstance(result, str):
        parts = result.strip().split()
        if len(parts) >= 2 and parts[0].upper() == "UPDATE":
            try:
                return int(parts[1])
            except (ValueError, IndexError):
                pass
    if isinstance(result, int):
        return result
    return 0
