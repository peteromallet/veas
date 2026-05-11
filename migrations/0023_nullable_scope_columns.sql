BEGIN;

-- ============================================================
-- Sprint 1: Add nullable scope columns to existing tables.
-- ALL columns are nullable — zero new NOT NULL constraints.
-- ============================================================

-- ----- messages -----
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS topic_id uuid REFERENCES topics(id),
    ADD COLUMN IF NOT EXISTS bot_id text REFERENCES bots(id);

-- ----- bot_turns -----
ALTER TABLE bot_turns
    ADD COLUMN IF NOT EXISTS topic_id uuid REFERENCES topics(id),
    ADD COLUMN IF NOT EXISTS bot_id text REFERENCES bots(id),
    ADD COLUMN IF NOT EXISTS bot_spec_version text,
    ADD COLUMN IF NOT EXISTS hot_context_builder_version text,
    ADD COLUMN IF NOT EXISTS tool_schema_version text;

-- ----- scheduled_jobs -----
ALTER TABLE scheduled_jobs
    ADD COLUMN IF NOT EXISTS topic_id uuid REFERENCES topics(id),
    ADD COLUMN IF NOT EXISTS bot_id text REFERENCES bots(id);

-- ----- feedback -----
ALTER TABLE feedback
    ADD COLUMN IF NOT EXISTS topic_id uuid REFERENCES topics(id),
    ADD COLUMN IF NOT EXISTS bot_id text REFERENCES bots(id);

-- ----- bridge_candidates -----
ALTER TABLE bridge_candidates
    ADD COLUMN IF NOT EXISTS topic_id uuid REFERENCES topics(id),
    ADD COLUMN IF NOT EXISTS bot_id text REFERENCES bots(id),
    ADD COLUMN IF NOT EXISTS dyad_id uuid REFERENCES dyads(id);

-- ----- memories -----
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS recorded_by_bot_id text REFERENCES bots(id);

-- ----- themes -----
ALTER TABLE themes
    ADD COLUMN IF NOT EXISTS recorded_by_bot_id text REFERENCES bots(id);

-- ----- observations -----
ALTER TABLE observations
    ADD COLUMN IF NOT EXISTS recorded_by_bot_id text REFERENCES bots(id);

-- ----- watch_items -----
ALTER TABLE watch_items
    ADD COLUMN IF NOT EXISTS recorded_by_bot_id text REFERENCES bots(id);

-- ----- out_of_bounds -----
ALTER TABLE out_of_bounds
    ADD COLUMN IF NOT EXISTS recorded_by_bot_id text REFERENCES bots(id);

-- ----- distillations -----
ALTER TABLE distillations
    ADD COLUMN IF NOT EXISTS recorded_by_bot_id text REFERENCES bots(id);

-- ============================================================
-- Confirm: no column above uses NOT NULL.
-- Every ADD COLUMN IF NOT EXISTS adds a nullable column.
-- ============================================================

COMMIT;