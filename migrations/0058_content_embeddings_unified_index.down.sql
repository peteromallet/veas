-- 0058_content_embeddings_unified_index.down: Reverse 0058_content_embeddings_unified_index.
--
-- Teardown order is deliberate: drop views before columns and tables, remove
-- message-delete cleanup before restoring legacy storage, drop generalized
-- embed-job objects, restore mediator.message_embeddings(message_id), then
-- reattach the message FK cascade and message-specific indexes. The message_id compatibility column is preserved.
--
-- Deploy compatibility window reversal:
--   * Generalized non-message jobs cannot be represented in the 0057
--     message-only table shape, so they are removed before source columns are
--     dropped.
--   * Message jobs are restored to message_id-only identity before the legacy
--     active dedupe index is recreated.

BEGIN;

DROP VIEW IF EXISTS mediator.v_searchable_messages;
DROP VIEW IF EXISTS mediator.v_searchable_content;

DROP TRIGGER IF EXISTS trg_messages_cleanup_content_embedding ON mediator.messages;
DROP FUNCTION IF EXISTS mediator.cleanup_message_content_embedding();

DROP TRIGGER IF EXISTS trg_embed_jobs_populate_source_identity ON mediator.embed_jobs;
DROP FUNCTION IF EXISTS mediator.populate_embed_job_source_identity();

DROP INDEX IF EXISTS mediator.idx_embed_jobs_active_source_dedupe;
DROP INDEX IF EXISTS mediator.idx_embed_jobs_source_status;
DROP INDEX IF EXISTS mediator.idx_embed_jobs_message_status;

-- Generalized non-message jobs have no legacy message_id representation.
DELETE FROM mediator.embed_jobs
WHERE source_type <> 'message';

UPDATE mediator.embed_jobs
SET message_id = source_id
WHERE source_type = 'message'
  AND message_id IS NULL;

ALTER TABLE mediator.embed_jobs
    DROP CONSTRAINT IF EXISTS embed_jobs_message_source_compat_check,
    DROP CONSTRAINT IF EXISTS embed_jobs_source_type_check,
    ALTER COLUMN message_id SET NOT NULL,
    DROP COLUMN IF EXISTS source_id,
    DROP COLUMN IF EXISTS source_type;

CREATE INDEX IF NOT EXISTS idx_embed_jobs_message_status
    ON mediator.embed_jobs (message_id, status, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_embed_jobs_active_dedupe
    ON mediator.embed_jobs (message_id, job_kind, COALESCE(content_hash, ''))
    WHERE status IN ('pending','processing');

DROP INDEX IF EXISTS mediator.idx_content_embeddings_embedded_at;
DROP INDEX IF EXISTS mediator.idx_content_embeddings_model_dimension;
DROP INDEX IF EXISTS mediator.idx_content_embeddings_source;

-- Only message rows can be represented by the legacy table.
DELETE FROM mediator.content_embeddings
WHERE source_type <> 'message';

ALTER TABLE mediator.content_embeddings
    DROP CONSTRAINT IF EXISTS content_embeddings_source_type_check,
    DROP CONSTRAINT IF EXISTS content_embeddings_pkey,
    ALTER COLUMN source_type DROP DEFAULT;

ALTER TABLE mediator.content_embeddings RENAME COLUMN source_id TO message_id;
ALTER TABLE mediator.content_embeddings DROP COLUMN source_type;
ALTER TABLE mediator.content_embeddings RENAME TO message_embeddings;

ALTER TABLE mediator.message_embeddings
    ADD CONSTRAINT message_embeddings_pkey PRIMARY KEY (message_id),
    ADD CONSTRAINT message_embeddings_message_id_fkey
        FOREIGN KEY (message_id) REFERENCES mediator.messages(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_message_embeddings_model_dimension
    ON mediator.message_embeddings (model, dimension);

CREATE INDEX IF NOT EXISTS idx_message_embeddings_embedded_at
    ON mediator.message_embeddings (embedded_at);

CREATE OR REPLACE VIEW mediator.v_searchable_messages AS
SELECT
    m.id AS message_id,
    m.direction,
    m.sender_id,
    m.recipient_id,
    CASE
        WHEN m.direction = 'inbound' AND m.sender_id IS NOT NULL THEN m.sender_id
        WHEN m.direction = 'outbound' AND m.recipient_id IS NOT NULL THEN m.recipient_id
        ELSE COALESCE(m.sender_id, m.recipient_id)
    END AS thread_owner_user_id,
    m.sent_at,
    COALESCE(m.charge, 'routine') AS charge,
    m.edited_at,
    m.edit_history,
    m.content,
    m.media_type,
    m.media_analysis,
    m.bot_id,
    m.topic_id,
    bb.dyad_id,
    ubs.partner_share AS thread_owner_partner_share,
    COALESCE(m.content, '') || E'\n' ||
        COALESCE(m.media_analysis->>'explanation', '') || E'\n' ||
        COALESCE(m.media_analysis->>'description', '') || E'\n' ||
        COALESCE(m.media_analysis->>'summary', '') AS canonical_text,
    m.search_tsv
FROM mediator.messages m
LEFT JOIN mediator.bot_bindings bb
  ON bb.bot_id = m.bot_id
 AND bb.dyad_id IS NOT NULL
LEFT JOIN mediator.user_bot_state ubs
  ON ubs.user_id = CASE
        WHEN m.direction = 'inbound' AND m.sender_id IS NOT NULL THEN m.sender_id
        WHEN m.direction = 'outbound' AND m.recipient_id IS NOT NULL THEN m.recipient_id
        ELSE COALESCE(m.sender_id, m.recipient_id)
    END
 AND ubs.bot_id = m.bot_id
WHERE m.deleted_at IS NULL
  AND m.search_suppressed_at IS NULL;

COMMENT ON VIEW mediator.v_searchable_messages IS
    'M1 retrieval read surface restored to the message-specific 0057 shape after reversing 0058.';

COMMIT;
