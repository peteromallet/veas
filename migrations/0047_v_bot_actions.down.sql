-- 0047_v_bot_actions.down: Drop mediator.v_bot_actions.
--
-- The view has no dependents inside the database (the application reads
-- it directly), so a plain DROP VIEW is sufficient.

BEGIN;

DROP VIEW IF EXISTS mediator.v_bot_actions;

COMMIT;
