-- 0050_habits_topic: Seed habits topic + Habits bot row (idempotent).
-- Mirrors 0037_fitness_topic.sql so the habits bot shares the commitments/
-- events substrate added in 0038. Must run before any commitments rows are
-- inserted against bot_id='habits'.
BEGIN;

INSERT INTO mediator.topics (id, slug, display_name, participants_shape)
VALUES (gen_random_uuid(), 'habits', 'Habits', 'solo')
ON CONFLICT (slug) DO NOTHING;

-- Backfill: if an earlier deploy seeded this row with the table default
-- 'dyad', flip it to 'solo' to match build_habits_spec()'s declaration.
UPDATE mediator.topics
   SET participants_shape = 'solo'
 WHERE slug = 'habits'
   AND participants_shape <> 'solo';

INSERT INTO mediator.bots (id, display_name)
VALUES ('habits', 'Habits')
ON CONFLICT (id) DO NOTHING;

COMMIT;
