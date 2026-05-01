BEGIN;

-- Supabase/Postgres schema foundation.
-- Service-role connections bypass RLS; policies below deny anon by default as defense in depth.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    phone text NOT NULL UNIQUE,
    timezone text NOT NULL,
    style_notes text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    direction text NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    sender_id uuid REFERENCES users(id),
    recipient_id uuid REFERENCES users(id),
    content text,
    sent_at timestamptz NOT NULL DEFAULT now(),
    in_reply_to uuid REFERENCES messages(id),
    processing_state text NOT NULL DEFAULT 'raw' CHECK (processing_state IN ('raw', 'processed', 'withheld', 'expired')),
    charge text NOT NULL DEFAULT 'routine' CHECK (charge IN ('routine', 'notable', 'charged', 'crisis')),
    media_url text,
    media_type text CHECK (media_type IS NULL OR media_type IN ('voice', 'image', 'document')),
    media_duration_seconds integer CHECK (media_duration_seconds IS NULL OR media_duration_seconds >= 0),
    media_analysis jsonb,
    whatsapp_message_id text UNIQUE,
    edited_at timestamptz,
    edit_history jsonb,
    deleted_at timestamptz
);

CREATE TABLE IF NOT EXISTS memories (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    about_user_id uuid REFERENCES users(id),
    content text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'invalidated')),
    supersedes_memory_id uuid REFERENCES memories(id),
    related_theme_ids uuid[],
    created_at timestamptz NOT NULL DEFAULT now(),
    last_referenced_at timestamptz
);

CREATE TABLE IF NOT EXISTS themes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title text NOT NULL,
    description text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'dormant', 'resolved', 'resolved_by_time')),
    sentiment text NOT NULL DEFAULT 'mixed' CHECK (sentiment IN ('improving', 'stable', 'worsening', 'mixed')),
    health text NOT NULL DEFAULT 'tender' CHECK (health IN ('healthy', 'tender', 'strained', 'inflamed')),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_active_at timestamptz NOT NULL DEFAULT now(),
    last_reinforced_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS watch_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id uuid NOT NULL REFERENCES users(id),
    content text NOT NULL,
    due_at timestamptz,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'addressed', 'expired', 'cancelled')),
    addressing_note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    addressed_at timestamptz,
    related_theme_ids uuid[]
);

CREATE TABLE IF NOT EXISTS observations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content text NOT NULL,
    about_user_id uuid REFERENCES users(id),
    confidence text NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    significance integer CHECK (significance BETWEEN 1 AND 5),
    scoring_prompt_version text,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'contradicted', 'stale')),
    related_theme_ids uuid[],
    supporting_message_ids uuid[],
    created_at timestamptz NOT NULL DEFAULT now(),
    last_reinforced_at timestamptz,
    surfaced_count integer NOT NULL DEFAULT 0 CHECK (surfaced_count >= 0)
);

CREATE TABLE IF NOT EXISTS out_of_bounds (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES users(id),
    sensitive_core text NOT NULL,
    shareable_context text,
    severity text NOT NULL CHECK (severity IN ('soft', 'firm', 'hard')),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'lifted')),
    created_at timestamptz NOT NULL DEFAULT now(),
    review_at timestamptz
);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES users(id),
    job_type text NOT NULL CHECK (job_type IN ('checkin', 'weekly_summary', 'oob_review', 'watch_item_due')),
    scheduled_for timestamptz NOT NULL,
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'fired', 'superseded', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    fired_at timestamptz
);

CREATE TABLE IF NOT EXISTS bot_turns (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    triggered_by_message_id uuid REFERENCES messages(id),
    user_in_context uuid REFERENCES users(id),
    prompt_snapshot text NOT NULL,
    system_prompt_version text NOT NULL,
    reasoning text,
    final_output_message_id uuid REFERENCES messages(id),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    model_version text NOT NULL,
    tool_call_count integer NOT NULL DEFAULT 0 CHECK (tool_call_count >= 0),
    duration_ms integer CHECK (duration_ms IS NULL OR duration_ms >= 0),
    failure_reason text
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id uuid NOT NULL REFERENCES bot_turns(id),
    tool_name text NOT NULL,
    arguments jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    called_at timestamptz NOT NULL DEFAULT now(),
    duration_ms integer CHECK (duration_ms IS NULL OR duration_ms >= 0)
);

CREATE TABLE IF NOT EXISTS feedback (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    from_user_id uuid NOT NULL REFERENCES users(id),
    target_type text NOT NULL CHECK (target_type IN ('message', 'turn', 'general')),
    target_id uuid,
    sentiment text NOT NULL CHECK (sentiment IN ('positive', 'negative', 'mixed')),
    content text,
    source text NOT NULL DEFAULT 'conversational' CHECK (source IN ('conversational', 'reaction')),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Operational spend counter, outside the spec's 11 application tables.
CREATE TABLE IF NOT EXISTS llm_spend_log (
    provider text NOT NULL,
    day date NOT NULL,
    total_usd numeric(10, 4) NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, day)
);

CREATE INDEX IF NOT EXISTS idx_messages_sender_sent_at ON messages (sender_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_processing_state_raw ON messages (processing_state) WHERE processing_state = 'raw';
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_pending ON scheduled_jobs (status, scheduled_for) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_themes_status ON themes (status);
CREATE INDEX IF NOT EXISTS idx_themes_last_reinforced_at ON themes (last_reinforced_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_active_last_reinforced_at ON observations (status, last_reinforced_at DESC) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_observations_about_user_status ON observations (about_user_id, status);
CREATE INDEX IF NOT EXISTS idx_memories_active_about_user_status ON memories (about_user_id, status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_watch_items_open_owner_status ON watch_items (owner_user_id, status) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_watch_items_due_at_not_null ON watch_items (due_at) WHERE due_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bot_turns_started_at ON bot_turns (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_bot_turns_triggered_by_message_id ON bot_turns (triggered_by_message_id);
CREATE INDEX IF NOT EXISTS idx_out_of_bounds_active_owner_status ON out_of_bounds (owner_id, status) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_observations_related_theme_ids_gin ON observations USING gin (related_theme_ids);
CREATE INDEX IF NOT EXISTS idx_observations_supporting_message_ids_gin ON observations USING gin (supporting_message_ids);
CREATE INDEX IF NOT EXISTS idx_memories_related_theme_ids_gin ON memories USING gin (related_theme_ids);
CREATE INDEX IF NOT EXISTS idx_watch_items_related_theme_ids_gin ON watch_items USING gin (related_theme_ids);

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE themes ENABLE ROW LEVEL SECURITY;
ALTER TABLE watch_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE out_of_bounds ENABLE ROW LEVEL SECURITY;
ALTER TABLE scheduled_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_turns ENABLE ROW LEVEL SECURITY;
ALTER TABLE tool_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_spend_log ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    CREATE POLICY deny_anon_users ON users FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_messages ON messages FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_memories ON memories FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_themes ON themes FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_watch_items ON watch_items FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_observations ON observations FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_out_of_bounds ON out_of_bounds FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_scheduled_jobs ON scheduled_jobs FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_bot_turns ON bot_turns FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_tool_calls ON tool_calls FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_feedback ON feedback FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_llm_spend_log ON llm_spend_log FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
