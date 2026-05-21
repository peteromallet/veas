-- 0054_artifact_links_widen_checks.down.sql — Reverse migration.
--
-- Restores the UNIQUE constraint on (artifact_id, target_table, target_id,
-- relation) and reverts the CHECK constraints to their 0051 values.
--
-- IMPORTANT — duplicate handling:
--   Sprint 4 writes produce multiple artifact_links rows for the same
--   (artifact_id, target_table, target_id, relation) tuple (distinct
--   evidence payloads per successful write).  Restoring the UNIQUE
--   constraint requires removing those duplicates first.
--
--   This down migration resolves duplicates by keeping the EARLIEST
--   (oldest created_at) row per group and dropping the rest.  A NOTICE
--   is emitted with the count of removed rows.  This is inherently lossy
--   — the discarded evidence rows cannot be recovered by re-running the
--   forward migration.  If this is unacceptable, run a manual audit
--   before applying the down migration.
--
--   If no duplicates exist (pre-Sprint-4 state), the UNIQUE constraint
--   is restored without data loss.
--
-- Sections:
--   1. Drop the forward-lookup index added in the up migration.
--   2. Remove duplicate artifact_links rows (keep earliest per group).
--   3. Restore the UNIQUE constraint.
--   4. Revert the target_table CHECK to 0051 values.
--   5. Revert the relation CHECK to 0051 values.

BEGIN;

-- ===========================================================================
-- 1. Drop forward-lookup index
-- ===========================================================================

DROP INDEX IF EXISTS mediator.idx_artifact_links_artifact_id;

-- ===========================================================================
-- 2. Remove duplicate artifact_links rows (keep earliest per group)
-- ===========================================================================

DO $$
DECLARE
    _removed integer;
BEGIN
    WITH duplicates AS (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY artifact_id, target_table, target_id, relation
                   ORDER BY created_at ASC
               ) AS rn
        FROM mediator.artifact_links
        WHERE deleted_at IS NULL
    )
    DELETE FROM mediator.artifact_links
    WHERE id IN (SELECT id FROM duplicates WHERE rn > 1);
    GET DIAGNOSTICS _removed = ROW_COUNT;
    IF _removed > 0 THEN
        RAISE WARNING '0054 down: removed % duplicate artifact_links rows '
                      'to restore uniqueness.  This data is lost and cannot '
                      'be recovered by re-running the forward migration.',
                      _removed;
    ELSE
        RAISE NOTICE '0054 down: no duplicate artifact_links rows found.  '
                     'Uniqueness can be restored without data loss.';
    END IF;
END $$;

-- ===========================================================================
-- 3. Restore the UNIQUE constraint on (artifact_id, target_table, target_id, relation)
-- ===========================================================================

ALTER TABLE mediator.artifact_links
    ADD UNIQUE (artifact_id, target_table, target_id, relation);

-- ===========================================================================
-- 4. Revert target_table CHECK to 0051 values
-- ===========================================================================

DO $$
DECLARE
    _conname text;
BEGIN
    SELECT c.conname INTO _conname
    FROM pg_constraint c
    JOIN pg_namespace n ON n.oid = c.connamespace
    WHERE n.nspname = 'mediator'
      AND c.conrelid = 'mediator.artifact_links'::regclass
      AND c.contype = 'c'
      AND c.conname = 'artifact_links_target_table_check';
    IF FOUND THEN
        EXECUTE format('ALTER TABLE mediator.artifact_links DROP CONSTRAINT %I', _conname);
    END IF;
END $$;

ALTER TABLE mediator.artifact_links
    ADD CONSTRAINT artifact_links_target_table_check
    CHECK (target_table IN (
        'conversations', 'conversation_items', 'transcript_turns',
        'conversation_notes', 'messages', 'memories', 'observations',
        'distillations', 'commitments', 'events', 'scheduled_jobs',
        'topic_status'
    ));

-- ===========================================================================
-- 5. Revert relation CHECK to 0051 values
-- ===========================================================================

DO $$
DECLARE
    _conname text;
BEGIN
    SELECT c.conname INTO _conname
    FROM pg_constraint c
    JOIN pg_namespace n ON n.oid = c.connamespace
    WHERE n.nspname = 'mediator'
      AND c.conrelid = 'mediator.artifact_links'::regclass
      AND c.contype = 'c'
      AND c.conname = 'artifact_links_relation_check';
    IF FOUND THEN
        EXECUTE format('ALTER TABLE mediator.artifact_links DROP CONSTRAINT %I', _conname);
    END IF;
END $$;

ALTER TABLE mediator.artifact_links
    ADD CONSTRAINT artifact_links_relation_check
    CHECK (relation IN (
        'planned_item', 'summarized_from', 'evidence_quote',
        'extracted_memory', 'extracted_observation', 'extracted_distillation',
        'created_commitment', 'logged_event', 'created_follow_up',
        'updated_topic_status'
    ));

COMMIT;
