-- 0048_inbound_handling_attempts.down: drop the attempt ledger.
--
-- The table has no foreign-key dependents inside the DB (bot_turns and
-- messages reference attempts the other way around through FK columns).
-- Dropping it cascades the indexes automatically.

BEGIN;

DROP TABLE IF EXISTS mediator.inbound_handling_attempts;

COMMIT;
