-- 0054_artifact_links_widen_checks: Drop uniqueness on artifact_links,
-- widen target_table + relation CHECK constraints for Sprint 4 provenance.
--
-- Changes:
--   1. Drop the UNIQUE (artifact_id, target_table, target_id, relation)
--      constraint created in 0051.  Sprint 4 explicitly allows multiple
--      evidence rows for the same artifact-target-relation tuple
--      (distinct evidence payloads per successful write).
--   2. Add a non-unique forward-lookup index on artifact_id so
--      list_artifact_links(artifact_id=...) stays efficient after the
--      uniqueness-backed index is removed.
--   3. Widen the target_table CHECK to include themes, watch_items,
--      out_of_bounds (added in T4).
--   4. Widen the relation CHECK to include the 10 new relations added
--      in T4 (extracted_theme, created/updated/closed_commitment,
--      updated_follow_up, created/updated/addressed_watch_item,
--      created/updated/lifted_oob).
--
-- The existing idx_artifact_links_target (target_table, target_id)
-- reverse-lookup index from 0051 is preserved — it is already non-unique
-- and correctly serves reverse-lookup queries.

BEGIN;

-- ===========================================================================
-- 1. Drop the UNIQUE constraint on (artifact_id, target_table, target_id, relation)
-- ===========================================================================
-- The constraint was created unnamed in 0051 line 95.  We discover and drop
-- it dynamically since PostgreSQL auto-generates the name.

DO $$
DECLARE
    _conname text;
BEGIN
    SELECT c.conname INTO _conname
    FROM pg_constraint c
    JOIN pg_namespace n ON n.oid = c.connamespace
    WHERE n.nspname = 'mediator'
      AND c.conrelid = 'mediator.artifact_links'::regclass
      AND c.contype = 'u';
    IF FOUND THEN
        EXECUTE format('ALTER TABLE mediator.artifact_links DROP CONSTRAINT %I', _conname);
        RAISE NOTICE '0054: dropped unique constraint % on mediator.artifact_links', _conname;
    ELSE
        RAISE NOTICE '0054: no unique constraint found on mediator.artifact_links (already dropped?)';
    END IF;
END $$;

-- ===========================================================================
-- 2. Non-unique forward-lookup index (compensates for removed unique index)
-- ===========================================================================

CREATE INDEX IF NOT EXISTS idx_artifact_links_artifact_id
    ON mediator.artifact_links (artifact_id);

-- ===========================================================================
-- 3. Widen target_table CHECK constraint
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
      AND pg_get_constraintdef(c.oid) ILIKE '%target_table%';
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
        'topic_status',
        'themes', 'watch_items', 'out_of_bounds'
    ));

-- ===========================================================================
-- 4. Widen relation CHECK constraint
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
      AND pg_get_constraintdef(c.oid) ILIKE '%relation%'
      AND pg_get_constraintdef(c.oid) NOT ILIKE '%target_table%';
    IF FOUND THEN
        EXECUTE format('ALTER TABLE mediator.artifact_links DROP CONSTRAINT %I', _conname);
    END IF;
END $$;

ALTER TABLE mediator.artifact_links
    ADD CONSTRAINT artifact_links_relation_check
    CHECK (relation IN (
        'planned_item', 'summarized_from', 'evidence_quote',
        'extracted_memory', 'extracted_observation', 'extracted_distillation',
        'extracted_theme',
        'created_commitment', 'updated_commitment', 'closed_commitment',
        'logged_event',
        'created_follow_up', 'updated_follow_up',
        'updated_topic_status',
        'created_watch_item', 'updated_watch_item', 'addressed_watch_item',
        'created_oob', 'updated_oob', 'lifted_oob'
    ));

COMMIT;
