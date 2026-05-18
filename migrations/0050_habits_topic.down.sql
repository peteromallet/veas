-- 0050_habits_topic down: Remove Habits bot row and habits topic.
-- Order matters because commitments.bot_id FKs mediator.bots(id); if any
-- commitments exist for bot_id='habits', the delete will be blocked — that
-- is intentional, the operator must drop the data first.
BEGIN;

DELETE FROM mediator.bots WHERE id = 'habits';
DELETE FROM mediator.topics WHERE slug = 'habits';

COMMIT;
