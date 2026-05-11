BEGIN;

-- ============================================================
-- Sprint 1: artifact_topics — polymorphic join table linking artifacts to topics
-- ============================================================

-- Polymorphic join: (artifact_table, artifact_id) identifies the artifact row.
-- No FK to individual artifact tables — orphan prevention is application
-- discipline; lint catches it in S2a.
CREATE TABLE IF NOT EXISTS artifact_topics (
    artifact_table text NOT NULL,
    artifact_id uuid NOT NULL,
    topic_id uuid NOT NULL REFERENCES topics(id),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    tagged_by_bot_id text REFERENCES bots(id),
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz,
    PRIMARY KEY (artifact_table, artifact_id, topic_id)
);

-- ============================================================
-- Partial unique indexes — enforce at most one active row per (artifact, topic).
-- Retired rows coexist freely (historical record).
-- ============================================================

-- Prevent duplicate active assignments for the same (artifact_table, artifact_id)
CREATE UNIQUE INDEX IF NOT EXISTS artifact_topics_active_artifact_key
    ON artifact_topics (artifact_table, artifact_id)
    WHERE status = 'active';

-- Prevent multiple active rows for the same (artifact_table, artifact_id, topic_id)
-- (covers the case where the same artifact is assigned to the same topic twice)
CREATE UNIQUE INDEX IF NOT EXISTS artifact_topics_active_unique_key
    ON artifact_topics (artifact_table, artifact_id, topic_id)
    WHERE status = 'active';

-- ============================================================
-- Indexes
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_artifact_topics_topic_status
    ON artifact_topics (topic_id, status);
CREATE INDEX IF NOT EXISTS idx_artifact_topics_tagged_by
    ON artifact_topics (tagged_by_bot_id) WHERE tagged_by_bot_id IS NOT NULL;

-- ============================================================
-- RLS: deny anon
-- ============================================================
ALTER TABLE artifact_topics ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifact_topics FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE artifact_topics FROM anon;

DO $$
BEGIN
    CREATE POLICY deny_anon_artifact_topics ON artifact_topics
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;