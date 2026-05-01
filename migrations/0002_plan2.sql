BEGIN;

ALTER TABLE messages ALTER COLUMN charge DROP NOT NULL;
ALTER TABLE messages ALTER COLUMN charge DROP DEFAULT;

ALTER TABLE bot_turns
    ADD COLUMN IF NOT EXISTS triggering_message_ids uuid[] NOT NULL DEFAULT '{}'::uuid[];

CREATE INDEX IF NOT EXISTS idx_bot_turns_triggering_message_ids_gin
    ON bot_turns USING gin (triggering_message_ids);

COMMIT;
