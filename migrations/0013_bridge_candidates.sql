BEGIN;

CREATE TABLE IF NOT EXISTS bridge_candidates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_user_id uuid NOT NULL REFERENCES users(id),
    target_user_id uuid NOT NULL REFERENCES users(id),
    kind text NOT NULL CHECK (kind IN ('context', 'clarification', 'contradiction', 'repair', 'vulnerability', 'process')),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready', 'sent', 'declined', 'blocked', 'addressed', 'expired')),
    sensitivity text NOT NULL DEFAULT 'medium' CHECK (sensitivity IN ('low', 'medium', 'high')),
    source_message_ids uuid[] NOT NULL DEFAULT '{}' CHECK (cardinality(source_message_ids) > 0),
    related_memory_ids uuid[] NOT NULL DEFAULT '{}',
    related_observation_ids uuid[] NOT NULL DEFAULT '{}',
    internal_note text NOT NULL DEFAULT '',
    shareable_summary text NOT NULL CHECK (length(btrim(shareable_summary)) > 0),
    sent_message_id uuid REFERENCES messages(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_bridge_candidates_source_target_newest
    ON bridge_candidates (source_user_id, target_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bridge_candidates_target_source_newest
    ON bridge_candidates (target_user_id, source_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bridge_candidates_target_visible_newest
    ON bridge_candidates (target_user_id, created_at DESC)
    WHERE status IN ('ready', 'sent', 'addressed');

CREATE INDEX IF NOT EXISTS idx_bridge_candidates_status_created
    ON bridge_candidates (status, created_at DESC);

REVOKE ALL ON TABLE bridge_candidates FROM anon, authenticated;

ALTER TABLE bridge_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE bridge_candidates FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    CREATE POLICY deny_anon_authenticated_bridge_candidates ON bridge_candidates
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
