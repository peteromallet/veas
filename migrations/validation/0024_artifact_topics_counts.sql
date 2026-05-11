-- ============================================================
-- Sprint 1: artifact_topics backfill validation queries.
-- Run AFTER scripts/backfill_artifact_topics.py completes.
-- Every query below MUST return 0 (or the expected count) for
-- the backfill to be considered valid.
-- ============================================================

-- ------------------------------------------------------------
-- 1. Per-table count parity: artifact_topics rows per table
--    must equal the source table's row count.
--    Each query returns (table, artifact_topics_count, source_count).
--    All rows should show equal counts.
-- ------------------------------------------------------------

-- memories
SELECT
    'memories' AS artifact_table,
    (SELECT count(*) FROM artifact_topics WHERE artifact_table = 'memories') AS at_count,
    (SELECT count(*) FROM memories) AS src_count;

-- themes
SELECT
    'themes' AS artifact_table,
    (SELECT count(*) FROM artifact_topics WHERE artifact_table = 'themes') AS at_count,
    (SELECT count(*) FROM themes) AS src_count;

-- observations
SELECT
    'observations' AS artifact_table,
    (SELECT count(*) FROM artifact_topics WHERE artifact_table = 'observations') AS at_count,
    (SELECT count(*) FROM observations) AS src_count;

-- watch_items
SELECT
    'watch_items' AS artifact_table,
    (SELECT count(*) FROM artifact_topics WHERE artifact_table = 'watch_items') AS at_count,
    (SELECT count(*) FROM watch_items) AS src_count;

-- out_of_bounds
SELECT
    'out_of_bounds' AS artifact_table,
    (SELECT count(*) FROM artifact_topics WHERE artifact_table = 'out_of_bounds') AS at_count,
    (SELECT count(*) FROM out_of_bounds) AS src_count;

-- distillations
SELECT
    'distillations' AS artifact_table,
    (SELECT count(*) FROM artifact_topics WHERE artifact_table = 'distillations') AS at_count,
    (SELECT count(*) FROM distillations) AS src_count;

-- ------------------------------------------------------------
-- 2. Summary: total artifact_topics versus sum of all source tables.
--    Diff must be zero.
-- ------------------------------------------------------------
SELECT
    (SELECT count(*) FROM artifact_topics) AS total_at,
    (
        (SELECT count(*) FROM memories) +
        (SELECT count(*) FROM themes) +
        (SELECT count(*) FROM observations) +
        (SELECT count(*) FROM watch_items) +
        (SELECT count(*) FROM out_of_bounds) +
        (SELECT count(*) FROM distillations)
    ) AS total_src,
    (SELECT count(*) FROM artifact_topics) - (
        (SELECT count(*) FROM memories) +
        (SELECT count(*) FROM themes) +
        (SELECT count(*) FROM observations) +
        (SELECT count(*) FROM watch_items) +
        (SELECT count(*) FROM out_of_bounds) +
        (SELECT count(*) FROM distillations)
    ) AS diff;

-- ------------------------------------------------------------
-- 3. OOB zero-result gate:
--    Every out_of_bounds row MUST have an artifact_topics row.
--    This query MUST return 0.
--    Locked decision §16.8: all existing OOB rows are
--    relationship-topic-scoped.  "No artifact_topics rows =
--    global OOB" is future-only; no current rows use it.
-- ------------------------------------------------------------
SELECT
    count(*) AS orphan_oob_count,
    CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL — OOB rows missing artifact_topics rows' END AS gate
FROM out_of_bounds
WHERE NOT EXISTS (
    SELECT 1 FROM artifact_topics
    WHERE artifact_table = 'out_of_bounds'
      AND artifact_id = out_of_bounds.id
);

-- ------------------------------------------------------------
-- 4. Identity gate:
--    user_identities with transport='legacy' count MUST equal
--    the number of users with a non-null phone column.
--    Diff must be zero.
-- ------------------------------------------------------------
SELECT
    (SELECT count(*) FROM user_identities WHERE transport = 'legacy') AS legacy_id_count,
    (SELECT count(*) FROM users WHERE phone IS NOT NULL) AS phone_user_count,
    (SELECT count(*) FROM user_identities WHERE transport = 'legacy') - (SELECT count(*) FROM users WHERE phone IS NOT NULL) AS diff;