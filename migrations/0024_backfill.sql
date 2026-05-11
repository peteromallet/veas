BEGIN;

-- ============================================================
-- Sprint 1: Resumable backfill progress tracking.
-- The migration_progress table is written by
-- scripts/backfill_artifact_topics.py (T7) so long-running
-- backfills can resume after interruption.
-- ============================================================

CREATE TABLE IF NOT EXISTS migration_progress (
    table_name        text PRIMARY KEY,
    last_id           uuid,
    backfill_started_at timestamptz,
    completed_at      timestamptz
);

-- RLS + anon-deny (defence in depth; service-role bypasses RLS)
ALTER TABLE migration_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE migration_progress FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE migration_progress FROM anon;

DO $$
BEGIN
    CREATE POLICY deny_anon_migration_progress
        ON migration_progress
        FOR ALL TO anon
        USING (false)
        WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;