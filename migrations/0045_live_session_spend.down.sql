BEGIN;
DROP INDEX IF EXISTS mediator.idx_conversations_spend_active;
ALTER TABLE mediator.conversations DROP COLUMN IF EXISTS spend_usd_cents;
COMMIT;
