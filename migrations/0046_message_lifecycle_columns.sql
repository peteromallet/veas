-- 0046_message_lifecycle_columns: Recovery-v2 lifecycle columns + writer-marker trigger.
--
-- Adds two nullable columns to mediator.messages that are mutated only by the
-- inbound_queue mutator helpers (claim_messages_for_turn / complete_messages /
-- fail_messages):
--
--   next_retry_at   timestamptz NULL  -- earliest moment a retryable failed row is eligible for re-claim
--   failure_class   text NULL         -- one of retryable_pre_send | terminal_post_send | infra_bug
--
-- A row-level BEFORE UPDATE trigger enforces that these columns can only
-- change inside a transaction that has set the txn-local GUC
-- ``app.lifecycle_writer = 'inbound_queue'`` via set_config(...).  This
-- catches accidental ad-hoc UPDATEs in other modules at write time.
--
-- The old idx_messages_inbound_failed_sweeper (from 0041) is dropped and
-- replaced by a retry partial index keyed on (bot_id, topic_id,
-- COALESCE(next_retry_at, sent_at)) that also excludes terminal failure
-- classes so the sweeper never re-touches terminal_post_send / infra_bug rows.
--
-- A new processing-state retention partial index supports the legacy
-- retention sweep on stale processing rows.
--
-- No backfill: existing failed rows have NULL failure_class and are treated
-- as retryable by the new index.  This is correct for the migration window.
--
-- Design decisions: see SD-A1-T1 through SD-A1-T8 in the agent-reliability
-- cleanup brief (docs/agent-reliability-cleanup-revised.md).

BEGIN;

-- 1. Add lifecycle columns ----------------------------------------------------
ALTER TABLE mediator.messages
    ADD COLUMN IF NOT EXISTS next_retry_at timestamptz NULL,
    ADD COLUMN IF NOT EXISTS failure_class text NULL;

-- 2. CHECK constraint on failure_class ----------------------------------------
ALTER TABLE mediator.messages
    ADD CONSTRAINT messages_failure_class_check
    CHECK (failure_class IS NULL OR failure_class IN (
        'retryable_pre_send',
        'terminal_post_send',
        'infra_bug'
    ));

-- 3. Replace 0041 failed-sweeper index with retry partial index ---------------
DROP INDEX IF EXISTS mediator.idx_messages_inbound_failed_sweeper;

CREATE INDEX IF NOT EXISTS idx_messages_inbound_failed_retry_sweeper
    ON mediator.messages (bot_id, topic_id, COALESCE(next_retry_at, sent_at))
    WHERE direction = 'inbound'
      AND processing_state = 'failed'
      AND (failure_class IS NULL
           OR failure_class NOT IN ('terminal_post_send', 'infra_bug'));

-- 4. Retention partial index on stale processing rows -------------------------
CREATE INDEX IF NOT EXISTS idx_messages_inbound_processing_retention
    ON mediator.messages (bot_id, topic_id, processing_started_at)
    WHERE direction = 'inbound'
      AND processing_state = 'processing';

-- 5. Writer-marker trigger ----------------------------------------------------
CREATE OR REPLACE FUNCTION mediator.assert_lifecycle_columns_writer()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (NEW.next_retry_at IS DISTINCT FROM OLD.next_retry_at
        OR NEW.failure_class IS DISTINCT FROM OLD.failure_class)
       AND current_setting('app.lifecycle_writer', true) IS DISTINCT FROM 'inbound_queue'
    THEN
        RAISE EXCEPTION
            'next_retry_at / failure_class may only be mutated by the inbound_queue '
            'mutator helpers (claim_messages_for_turn / complete_messages / fail_messages); '
            'caller must set_config(''app.lifecycle_writer'', ''inbound_queue'', true) in '
            'the same transaction.';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_messages_assert_lifecycle_writer ON mediator.messages;

CREATE TRIGGER trg_messages_assert_lifecycle_writer
    BEFORE UPDATE ON mediator.messages
    FOR EACH ROW
    EXECUTE FUNCTION mediator.assert_lifecycle_columns_writer();

COMMIT;
