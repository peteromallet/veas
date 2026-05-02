BEGIN;

-- Plan 5 scheduler foundation. Runtime workers use service-role database
-- connections; RLS still denies anon by default for defense in depth.

CREATE TABLE IF NOT EXISTS system_state (
    key text PRIMARY KEY,
    value jsonb NOT NULL DEFAULT '{}'::jsonb,
    paused_at timestamptz,
    paused_by_user_id uuid REFERENCES users(id),
    pause_reason text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        key <> 'global_pause'
        OR (
            paused_at IS NULL
            OR jsonb_typeof(value) = 'object'
        )
    )
);

INSERT INTO system_state (key, value)
VALUES ('global_pause', '{}'::jsonb)
ON CONFLICT (key) DO NOTHING;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS weekly_summary_enabled boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS weekly_summary_day smallint NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS weekly_summary_time time NOT NULL DEFAULT TIME '09:00';

DO $$
BEGIN
    ALTER TABLE users
        ADD CONSTRAINT users_weekly_summary_day_check
        CHECK (weekly_summary_day BETWEEN 0 AND 6);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE observations
    ADD COLUMN IF NOT EXISTS needs_rescoring boolean NOT NULL DEFAULT false;

ALTER TABLE scheduled_jobs
    ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 2,
    ADD COLUMN IF NOT EXISTS last_error text,
    ADD COLUMN IF NOT EXISTS cancellation_reason text,
    ADD COLUMN IF NOT EXISTS claimed_at timestamptz,
    ADD COLUMN IF NOT EXISTS claimed_by text,
    ADD COLUMN IF NOT EXISTS delayed boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

DO $$
BEGIN
    ALTER TABLE scheduled_jobs
        ADD CONSTRAINT scheduled_jobs_attempt_count_check
        CHECK (attempt_count >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE scheduled_jobs
        ADD CONSTRAINT scheduled_jobs_max_attempts_check
        CHECK (max_attempts >= 1);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE scheduled_jobs DROP CONSTRAINT IF EXISTS scheduled_jobs_job_type_check;
ALTER TABLE scheduled_jobs
    ADD CONSTRAINT scheduled_jobs_job_type_check
    CHECK (job_type IN ('checkin', 'weekly_summary', 'oob_review', 'watch_item_due', 'heartbeat', 'deferred_turn'));

CREATE OR REPLACE FUNCTION recency_weighted_score(
    significance integer,
    last_reinforced_at timestamptz,
    created_at timestamptz
) RETURNS numeric
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN significance IS NULL THEN NULL
        ELSE significance::numeric / (
            1 + (
                GREATEST(
                    0,
                    EXTRACT(EPOCH FROM (now() - COALESCE(last_reinforced_at, created_at, now()))) / 86400
                ) / 60
            )
        )
    END;
$$;

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_pending_due_claim
    ON scheduled_jobs (scheduled_for, created_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_pending_type_due
    ON scheduled_jobs (job_type, scheduled_for)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_user_type_status
    ON scheduled_jobs (user_id, job_type, status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_jobs_one_pending_checkin_per_user
    ON scheduled_jobs (user_id)
    WHERE status = 'pending' AND job_type = 'checkin' AND user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_pending_heartbeat
    ON scheduled_jobs (scheduled_for)
    WHERE status = 'pending' AND job_type = 'heartbeat';

CREATE INDEX IF NOT EXISTS idx_users_weekly_summary_schedule
    ON users (weekly_summary_enabled, weekly_summary_day, weekly_summary_time)
    WHERE weekly_summary_enabled = true;

CREATE INDEX IF NOT EXISTS idx_system_state_paused_at
    ON system_state (paused_at)
    WHERE key = 'global_pause';

CREATE INDEX IF NOT EXISTS idx_observations_needs_rescoring
    ON observations (needs_rescoring, status, last_reinforced_at)
    WHERE needs_rescoring = true;

-- The weighted score uses now(), so it cannot be indexed directly. This index
-- supports the same default surfacing filter before applying the score.
CREATE INDEX IF NOT EXISTS idx_observations_active_significance_reinforced
    ON observations (significance DESC NULLS LAST, COALESCE(last_reinforced_at, created_at) DESC)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_themes_decay_active_last_reinforced
    ON themes (status, COALESCE(last_reinforced_at, first_seen_at), last_active_at)
    WHERE status IN ('active', 'dormant');

CREATE INDEX IF NOT EXISTS idx_watch_items_expiry
    ON watch_items (status, due_at)
    WHERE status = 'open' AND due_at IS NOT NULL;

ALTER TABLE system_state ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    CREATE POLICY deny_anon_system_state ON system_state FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
