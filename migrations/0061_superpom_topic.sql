-- 0061_superpom_topic: Seed superpom topic + SuperPOM bot row (idempotent).
-- Mirrors 0037_fitness_topic.sql and 0050_habits_topic.sql so the SuperPOM bot
-- shares the commitments/events substrate added in 0038. Must run before any
-- commitments rows are inserted against bot_id='superpom'.
BEGIN;

INSERT INTO mediator.topics (id, slug, display_name, participants_shape)
VALUES (gen_random_uuid(), 'superpom', 'SuperPOM', 'solo')
ON CONFLICT (slug) DO NOTHING;

-- Backfill: if an earlier deploy seeded this row with the table default
-- 'dyad', flip it to 'solo' to match the SuperPOM BotSpec declaration.
UPDATE mediator.topics
   SET participants_shape = 'solo'
 WHERE slug = 'superpom'
   AND participants_shape <> 'solo';

INSERT INTO mediator.bots (id, display_name)
VALUES ('superpom', 'SuperPOM')
ON CONFLICT (id) DO NOTHING;

COMMIT;
