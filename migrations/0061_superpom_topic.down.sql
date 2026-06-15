-- 0061_superpom_topic down: Remove SuperPOM bot row and superpom topic.
-- Order matters because commitments.bot_id FKs mediator.bots(id); if any
-- commitments exist for bot_id='superpom', the delete will be blocked — that
-- is intentional, the operator must drop the data first.
BEGIN;

DELETE FROM mediator.bots WHERE id = 'superpom';
DELETE FROM mediator.topics WHERE slug = 'superpom';

COMMIT;
