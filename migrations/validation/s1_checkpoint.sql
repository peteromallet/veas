-- ============================================================
-- Sprint 1: Mid-sprint checkpoint queries (§16.1 checkpoint).
-- Run against staging AFTER migrations 0020–0024 are applied
-- and scripts/backfill_artifact_topics.py has completed.
-- ============================================================

-- ------------------------------------------------------------
-- CP1: artifact_topics total = sum of per-artifact source counts.
--     diff must be zero.
-- ------------------------------------------------------------
SELECT
    'CP1' AS checkpoint,
    (SELECT count(*) FROM artifact_topics) AS at_total,
    (
        (SELECT count(*) FROM memories) +
        (SELECT count(*) FROM themes) +
        (SELECT count(*) FROM observations) +
        (SELECT count(*) FROM watch_items) +
        (SELECT count(*) FROM out_of_bounds) +
        (SELECT count(*) FROM distillations)
    ) AS src_total,
    CASE
        WHEN (SELECT count(*) FROM artifact_topics) = (
            (SELECT count(*) FROM memories) +
            (SELECT count(*) FROM themes) +
            (SELECT count(*) FROM observations) +
            (SELECT count(*) FROM watch_items) +
            (SELECT count(*) FROM out_of_bounds) +
            (SELECT count(*) FROM distillations)
        ) THEN 'PASS'
        ELSE 'FAIL'
    END AS gate;

-- ------------------------------------------------------------
-- CP2: user_identities legacy count = users with phone count.
--     diff must be zero.
-- ------------------------------------------------------------
SELECT
    'CP2' AS checkpoint,
    (SELECT count(*) FROM user_identities WHERE transport = 'legacy') AS legacy_id_count,
    (SELECT count(*) FROM users WHERE phone IS NOT NULL) AS phone_user_count,
    CASE
        WHEN (SELECT count(*) FROM user_identities WHERE transport = 'legacy') =
             (SELECT count(*) FROM users WHERE phone IS NOT NULL)
        THEN 'PASS'
        ELSE 'FAIL'
    END AS gate;

-- ------------------------------------------------------------
-- CP3: OOB gate — every out_of_bounds row has an artifact_topics
--     row. Must return 0.
-- ------------------------------------------------------------
SELECT
    'CP3' AS checkpoint,
    (SELECT count(*) FROM out_of_bounds WHERE NOT EXISTS (
        SELECT 1 FROM artifact_topics
        WHERE artifact_table = 'out_of_bounds'
          AND artifact_id = out_of_bounds.id
    )) AS orphan_oob_count,
    CASE
        WHEN (SELECT count(*) FROM out_of_bounds WHERE NOT EXISTS (
            SELECT 1 FROM artifact_topics
            WHERE artifact_table = 'out_of_bounds'
              AND artifact_id = out_of_bounds.id
        )) = 0
        THEN 'PASS'
        ELSE 'FAIL'
    END AS gate;

-- ------------------------------------------------------------
-- CP4 (INFORMATIONAL ONLY for S1):
--     messages.bot_id IS NULL count.
--     This is expected to be NONZERO in S1 — the column was added
--     as nullable in 0023 but is NOT backfilled.  S2a makes the
--     gate meaningful when insert sites start writing it.
--     This query is for awareness only; no PASS/FAIL gate.
-- ------------------------------------------------------------
SELECT
    'CP4' AS checkpoint,
    (SELECT count(*) FROM messages WHERE bot_id IS NULL) AS null_bot_id_count,
    (SELECT count(*) FROM messages) AS total_messages,
    'INFORMATIONAL — bot_id not backfilled in S1; nonzero is expected' AS note;

-- ------------------------------------------------------------
-- CP5: Verify zero new NOT NULL constraints exist on altered tables.
--     Run this manually via psql \d+ on each table listed in 0023,
--     or check information_schema.
-- ------------------------------------------------------------
SELECT
    'CP5' AS checkpoint,
    column_name,
    data_type,
    is_nullable
FROM information_schema.columns
WHERE table_schema = 'mediator'
  AND table_name IN ('messages', 'bot_turns', 'scheduled_jobs', 'feedback', 'bridge_candidates',
                     'memories', 'themes', 'observations', 'watch_items', 'out_of_bounds', 'distillations')
  AND column_name IN ('topic_id', 'bot_id', 'dyad_id', 'bot_spec_version',
                      'hot_context_builder_version', 'tool_schema_version', 'recorded_by_bot_id')
ORDER BY table_name, column_name;