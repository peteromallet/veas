BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS onboarding_state text NOT NULL DEFAULT 'pending';

DO $$
BEGIN
    ALTER TABLE users
        ADD CONSTRAINT users_onboarding_state_check
        CHECK (onboarding_state IN ('pending', 'welcomed', 'seeded', 'complete'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_processing_state_check;
ALTER TABLE messages
    ADD CONSTRAINT messages_processing_state_check
    CHECK (processing_state IN ('raw', 'processed', 'withheld', 'expired', 'deferred'));

ALTER TABLE scheduled_jobs DROP CONSTRAINT IF EXISTS scheduled_jobs_job_type_check;
ALTER TABLE scheduled_jobs
    ADD CONSTRAINT scheduled_jobs_job_type_check
    CHECK (job_type IN ('checkin', 'weekly_summary', 'oob_review', 'watch_item_due', 'heartbeat', 'deferred_turn'));

ALTER TABLE llm_spend_log
    ADD COLUMN IF NOT EXISTS warned_80_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_users_onboarding_state
    ON users (onboarding_state)
    WHERE onboarding_state IN ('pending', 'welcomed', 'seeded');

CREATE INDEX IF NOT EXISTS idx_messages_deferred
    ON messages (sender_id, sent_at)
    WHERE direction = 'inbound' AND processing_state = 'deferred';

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_deferred_turn_pending
    ON scheduled_jobs (user_id, scheduled_for)
    WHERE status = 'pending' AND job_type = 'deferred_turn';

COMMIT;
