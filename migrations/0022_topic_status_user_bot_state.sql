BEGIN;

-- ============================================================
-- Sprint 1: topic_status — bot-authored summaries scoped to a topic+dyad or topic+user
--            user_bot_state — per-user, per-bot state (onboarding, paused)
-- ============================================================

-- 1. topic_status — each row captures a bot-authored status update scoped to
--    a topic and either a dyad or a single user (XOR constraint).
CREATE TABLE IF NOT EXISTS topic_status (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_id uuid NOT NULL REFERENCES topics(id),
    dyad_id uuid REFERENCES dyads(id),
    user_id uuid REFERENCES users(id),
    headline text NOT NULL,
    body text NOT NULL DEFAULT '',
    last_updated_at timestamptz NOT NULL DEFAULT now(),
    last_updated_by_bot_id text NOT NULL REFERENCES bots(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT topic_status_xor CHECK ((user_id IS NOT NULL) <> (dyad_id IS NOT NULL))
);

-- Partial unique indexes: at most one status row per (topic, user) or (topic, dyad)
CREATE UNIQUE INDEX IF NOT EXISTS topic_status_user_key
    ON topic_status (topic_id, user_id)
    WHERE user_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS topic_status_dyad_key
    ON topic_status (topic_id, dyad_id)
    WHERE dyad_id IS NOT NULL;

-- 2. user_bot_state — per-user, per-bot control state
--    onboarding_state mirrors the CHECK from users.onboarding_state
CREATE TABLE IF NOT EXISTS user_bot_state (
    user_id uuid NOT NULL REFERENCES users(id),
    bot_id text NOT NULL REFERENCES bots(id),
    onboarding_state text NOT NULL DEFAULT 'pending'
        CHECK (onboarding_state IN ('pending', 'welcomed', 'seeded', 'complete')),
    paused boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, bot_id)
);

-- ============================================================
-- Backfill user_bot_state from users.onboarding_state for mediator
-- ============================================================
INSERT INTO user_bot_state (user_id, bot_id, onboarding_state)
SELECT id, 'mediator', onboarding_state
FROM users
WHERE onboarding_state IS NOT NULL
ON CONFLICT (user_id, bot_id) DO NOTHING;

-- ============================================================
-- Indexes
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_topic_status_topic_last_updated
    ON topic_status (topic_id, last_updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_topic_status_updated_by_bot
    ON topic_status (last_updated_by_bot_id);
CREATE INDEX IF NOT EXISTS idx_user_bot_state_bot_onboarding
    ON user_bot_state (bot_id, onboarding_state);

-- ============================================================
-- RLS: deny anon on both tables
-- ============================================================

ALTER TABLE topic_status ENABLE ROW LEVEL SECURITY;
ALTER TABLE topic_status FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE topic_status FROM anon;

ALTER TABLE user_bot_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_bot_state FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE user_bot_state FROM anon;

DO $$
BEGIN
    CREATE POLICY deny_anon_topic_status ON topic_status
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_user_bot_state ON user_bot_state
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;