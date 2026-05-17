-- 0046_message_lifecycle_columns.down: Revert recovery-v2 lifecycle columns.
--
-- Order:
--   1. Drop trigger and function (depend on the columns).
--   2. Drop retention and retry partial indexes.
--   3. Drop the columns (CHECK constraint goes with the columns).
--   4. Recreate idx_messages_inbound_failed_sweeper verbatim from
--      migrations/0041_inbound_queue_handling.sql:72-74 so the legacy sweeper
--      index is restored.

BEGIN;

DROP TRIGGER IF EXISTS trg_messages_assert_lifecycle_writer ON mediator.messages;
DROP FUNCTION IF EXISTS mediator.assert_lifecycle_columns_writer();

DROP INDEX IF EXISTS mediator.idx_messages_inbound_processing_retention;
DROP INDEX IF EXISTS mediator.idx_messages_inbound_failed_retry_sweeper;

ALTER TABLE mediator.messages
    DROP CONSTRAINT IF EXISTS messages_failure_class_check;

ALTER TABLE mediator.messages
    DROP COLUMN IF EXISTS failure_class,
    DROP COLUMN IF EXISTS next_retry_at;

CREATE INDEX idx_messages_inbound_failed_sweeper
    ON mediator.messages (bot_id, topic_id, sent_at)
    WHERE direction='inbound' AND processing_state='failed';

COMMIT;
