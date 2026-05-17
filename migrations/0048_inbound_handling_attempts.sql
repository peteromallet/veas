-- 0048_inbound_handling_attempts: Per-attempt ledger for inbound message handling.
--
-- Project C, C2.  Per SD-009 (user override of SD-001), this ledger is
-- written but NOT read by the existing recovery / retry paths — they
-- continue to consult ``mediator.messages.next_retry_at`` and
-- ``mediator.messages.failure_class``.  Dual-write is gated by the
-- ``ledger_dual_write_enabled`` setting (default False) so flipping the
-- flag off reverts to messages-only writes without a redeploy.
--
-- Schema rationale
-- ----------------
-- One row per attempt at handling a given inbound message.  Multiple
-- attempts share ``message_id``; only one may have ``status='active'``
-- at a time (enforced by a partial unique index).  ``created_by``
-- records the path that opened the attempt:
--
--   live       a normal claim from a coalescer / pacer dispatch
--   catch_up   a startup reconciliation that found an in-flight row with
--              no active ledger entry
--   recovery   the recovery sweep that re-claims a previously failed row
--   manual     ad-hoc operator-driven recovery
--   backfill   the one-shot backfill script
--                (scripts/backfill_inbound_handling_attempts.py)
--
-- ``failure_class`` mirrors the wider taxonomy introduced in
-- app/services/failure_policy.py (Project C, C3); the column is plain
-- text so legacy values (``retryable_pre_send`` / ``terminal_post_send``
-- / ``infra_bug``) and the C3 additions coexist without a CHECK.
--
-- Indexes
-- -------
-- * ``idx_iha_message_id``      — joining attempts back to the message row.
-- * ``idx_iha_failed_retry``    — partial index on (status, next_retry_at)
--                                 WHERE status='failed' supports a future
--                                 ledger-driven retry path.
-- * ``idx_iha_one_active_per_message`` — partial UNIQUE index enforcing the
--                                 "at most one active attempt per message"
--                                 invariant.
--
-- Down migration drops the table; no dependents exist in the DB schema.

BEGIN;

CREATE TABLE IF NOT EXISTS mediator.inbound_handling_attempts (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id      uuid NOT NULL REFERENCES mediator.messages(id) ON DELETE CASCADE,
    bot_turn_id     uuid REFERENCES mediator.bot_turns(id),
    bot_id          text NOT NULL,
    topic_id        uuid NOT NULL,
    attempt_number  int  NOT NULL,
    status          text NOT NULL CHECK (status IN ('active','succeeded','failed','expired')),
    failure_class   text,
    failure_reason  text,
    started_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    next_retry_at   timestamptz,
    created_by      text NOT NULL CHECK (created_by IN ('live','catch_up','recovery','manual','backfill'))
);

CREATE INDEX IF NOT EXISTS idx_iha_message_id
    ON mediator.inbound_handling_attempts (message_id);

CREATE INDEX IF NOT EXISTS idx_iha_failed_retry
    ON mediator.inbound_handling_attempts (status, next_retry_at)
    WHERE status = 'failed';

CREATE UNIQUE INDEX IF NOT EXISTS idx_iha_one_active_per_message
    ON mediator.inbound_handling_attempts (message_id)
    WHERE status = 'active';

COMMENT ON TABLE mediator.inbound_handling_attempts IS
    'Per-attempt ledger for inbound message handling (Project C, C2). '
    'Dual-write target gated by the ledger_dual_write_enabled setting; '
    'recovery still reads messages.next_retry_at / messages.failure_class.';

COMMIT;
