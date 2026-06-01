-- 0058_content_embeddings_unified_index: Xen v2 M1 unified content embeddings.
--
-- This migration generalizes the existing message vector table in place.
-- Existing mediator.message_embeddings rows are preserved by renaming the
-- table to mediator.content_embeddings, renaming message_id to source_id, and
-- stamping those rows with source_type='message'. The old message-only FK and
-- PK are then replaced with the generalized composite identity.
--
-- Message-specific compatibility surfaces remain during M1:
--   * mediator.embed_jobs.message_id remains nullable compatibility metadata.
--   * mediator.v_searchable_messages is a message-only projection over
--     mediator.v_searchable_content.
--
-- Deploy compatibility window:
--   * Old message-only writers may continue inserting embed_jobs(message_id).
--     The trigger below derives source_type='message' and source_id=message_id.
--   * New generalized writers should insert source_type/source_id. When the
--     source_type is 'message', the trigger mirrors source_id back to
--     message_id so old message workers can still observe message jobs.
--   * Non-message jobs must leave message_id NULL. A later cleanup migration
--     can remove message_id after all workers read source_type/source_id.
--
-- M1 deliberately excludes dyad_shareable memories and distillations from the
-- unified index. Partner-share substitution/filtering for non-message return
-- paths belongs to a later milestone.

BEGIN;

ALTER TABLE mediator.message_embeddings RENAME TO content_embeddings;
ALTER TABLE mediator.content_embeddings RENAME COLUMN message_id TO source_id;

ALTER TABLE mediator.content_embeddings
    ADD COLUMN source_type text;

UPDATE mediator.content_embeddings
SET source_type = 'message'
WHERE source_type IS NULL;

ALTER TABLE mediator.content_embeddings
    ALTER COLUMN source_type SET NOT NULL,
    ALTER COLUMN source_type SET DEFAULT 'message',
    ADD CONSTRAINT content_embeddings_source_type_check
        CHECK (source_type IN ('message','memory','observation','distillation','artifact'));

DO $$
DECLARE
    constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'mediator.content_embeddings'::regclass
          AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE mediator.content_embeddings DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END;
$$;

DO $$
DECLARE
    constraint_name text;
BEGIN
    SELECT conname
      INTO constraint_name
      FROM pg_constraint
     WHERE conrelid = 'mediator.content_embeddings'::regclass
       AND contype = 'p';

    IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE mediator.content_embeddings DROP CONSTRAINT %I', constraint_name);
    END IF;
END;
$$;

DROP INDEX IF EXISTS mediator.idx_message_embeddings_model_dimension;
DROP INDEX IF EXISTS mediator.idx_message_embeddings_embedded_at;

ALTER TABLE mediator.content_embeddings
    ADD CONSTRAINT content_embeddings_pkey PRIMARY KEY (source_type, source_id);

CREATE INDEX IF NOT EXISTS idx_content_embeddings_source
    ON mediator.content_embeddings (source_type, source_id);

CREATE INDEX IF NOT EXISTS idx_content_embeddings_model_dimension
    ON mediator.content_embeddings (model, dimension);

CREATE INDEX IF NOT EXISTS idx_content_embeddings_embedded_at
    ON mediator.content_embeddings (embedded_at);

ALTER TABLE mediator.embed_jobs
    ADD COLUMN IF NOT EXISTS source_type text,
    ADD COLUMN IF NOT EXISTS source_id uuid;

-- message_id remains nullable compatibility metadata for old message-specific
-- callers while new jobs use source_type/source_id as the durable identity.
ALTER TABLE mediator.embed_jobs
    ALTER COLUMN message_id DROP NOT NULL;

UPDATE mediator.embed_jobs
SET source_type = COALESCE(source_type, 'message'),
    source_id = COALESCE(source_id, message_id)
WHERE source_id IS NULL
   OR source_type IS NULL;

UPDATE mediator.embed_jobs
SET message_id = source_id
WHERE source_type = 'message'
  AND message_id IS NULL;

ALTER TABLE mediator.embed_jobs
    ALTER COLUMN source_type SET NOT NULL,
    ALTER COLUMN source_type SET DEFAULT 'message',
    ALTER COLUMN source_id SET NOT NULL,
    ADD CONSTRAINT embed_jobs_source_type_check
        CHECK (source_type IN ('message','memory','observation','distillation','artifact')),
    ADD CONSTRAINT embed_jobs_message_source_compat_check
        CHECK (message_id IS NULL OR (source_type = 'message' AND source_id = message_id));

CREATE OR REPLACE FUNCTION mediator.populate_embed_job_source_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.source_type IS NULL AND NEW.message_id IS NOT NULL THEN
        NEW.source_type := 'message';
    END IF;

    IF NEW.source_id IS NULL AND NEW.message_id IS NOT NULL THEN
        NEW.source_id := NEW.message_id;
    END IF;

    IF NEW.source_type = 'message' AND NEW.message_id IS NULL AND NEW.source_id IS NOT NULL THEN
        NEW.message_id := NEW.source_id;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_embed_jobs_populate_source_identity ON mediator.embed_jobs;
CREATE TRIGGER trg_embed_jobs_populate_source_identity
BEFORE INSERT OR UPDATE OF message_id, source_type, source_id ON mediator.embed_jobs
FOR EACH ROW
EXECUTE FUNCTION mediator.populate_embed_job_source_identity();

DROP INDEX IF EXISTS mediator.idx_embed_jobs_message_status;
DROP INDEX IF EXISTS mediator.idx_embed_jobs_active_dedupe;

CREATE INDEX IF NOT EXISTS idx_embed_jobs_message_status
    ON mediator.embed_jobs (message_id, status, created_at DESC)
    WHERE message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_embed_jobs_source_status
    ON mediator.embed_jobs (source_type, source_id, status, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_embed_jobs_active_source_dedupe
    ON mediator.embed_jobs (
        source_type,
        source_id,
        job_kind,
        COALESCE(content_hash, '')
    )
    WHERE status IN ('pending','processing');

CREATE OR REPLACE VIEW mediator.v_searchable_content AS
SELECT
    'message'::text AS source_type,
    m.id AS source_id,
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
    m.search_tsv,
    m.sent_at AS sort_at,
    m.topic_id AS primary_topic_id,
    CASE WHEN m.topic_id IS NULL THEN ARRAY[]::uuid[] ELSE ARRAY[m.topic_id] END AS topic_ids,
    m.sent_at AS source_created_at,
    COALESCE(m.edited_at, m.sent_at) AS source_updated_at
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
  AND m.search_suppressed_at IS NULL

UNION ALL

SELECT
    'memory'::text AS source_type,
    mem.id AS source_id,
    NULL::uuid AS message_id,
    NULL::text AS direction,
    mem.about_user_id AS sender_id,
    NULL::uuid AS recipient_id,
    mem.about_user_id AS thread_owner_user_id,
    mem.created_at AS sent_at,
    'routine'::text AS charge,
    NULL::timestamptz AS edited_at,
    NULL::jsonb AS edit_history,
    mem.content,
    NULL::text AS media_type,
    NULL::jsonb AS media_analysis,
    mem.recorded_by_bot_id AS bot_id,
    topics.primary_topic_id AS topic_id,
    NULL::uuid AS dyad_id,
    NULL::boolean AS thread_owner_partner_share,
    mem.content AS canonical_text,
    to_tsvector('simple'::regconfig, COALESCE(mem.content, '')) AS search_tsv,
    mem.created_at AS sort_at,
    topics.primary_topic_id,
    COALESCE(topics.topic_ids, ARRAY[]::uuid[]) AS topic_ids,
    mem.created_at AS source_created_at,
    COALESCE(mem.last_referenced_at, mem.created_at) AS source_updated_at
FROM mediator.memories mem
LEFT JOIN LATERAL (
    SELECT
        (array_agg(at.topic_id ORDER BY at.topic_id))[1] AS primary_topic_id,
        array_agg(at.topic_id ORDER BY at.topic_id) AS topic_ids
    FROM mediator.artifact_topics at
    WHERE at.artifact_table = 'memories'
      AND at.artifact_id = mem.id
      AND at.status = 'active'
) topics ON true
WHERE mem.status = 'active'
  AND COALESCE(mem.visibility, 'private') = 'private'

UNION ALL

SELECT
    'observation'::text AS source_type,
    obs.id AS source_id,
    NULL::uuid AS message_id,
    NULL::text AS direction,
    obs.about_user_id AS sender_id,
    NULL::uuid AS recipient_id,
    obs.about_user_id AS thread_owner_user_id,
    obs.created_at AS sent_at,
    'routine'::text AS charge,
    NULL::timestamptz AS edited_at,
    NULL::jsonb AS edit_history,
    obs.content,
    NULL::text AS media_type,
    NULL::jsonb AS media_analysis,
    obs.recorded_by_bot_id AS bot_id,
    topics.primary_topic_id AS topic_id,
    NULL::uuid AS dyad_id,
    NULL::boolean AS thread_owner_partner_share,
    obs.content AS canonical_text,
    to_tsvector('simple'::regconfig, COALESCE(obs.content, '')) AS search_tsv,
    obs.created_at AS sort_at,
    topics.primary_topic_id,
    COALESCE(topics.topic_ids, ARRAY[]::uuid[]) AS topic_ids,
    obs.created_at AS source_created_at,
    COALESCE(obs.last_reinforced_at, obs.created_at) AS source_updated_at
FROM mediator.observations obs
LEFT JOIN LATERAL (
    SELECT
        (array_agg(at.topic_id ORDER BY at.topic_id))[1] AS primary_topic_id,
        array_agg(at.topic_id ORDER BY at.topic_id) AS topic_ids
    FROM mediator.artifact_topics at
    WHERE at.artifact_table = 'observations'
      AND at.artifact_id = obs.id
      AND at.status = 'active'
) topics ON true
WHERE obs.status = 'active'
  AND obs.significance >= 3

UNION ALL

SELECT
    'distillation'::text AS source_type,
    d.id AS source_id,
    NULL::uuid AS message_id,
    NULL::text AS direction,
    NULL::uuid AS sender_id,
    NULL::uuid AS recipient_id,
    NULL::uuid AS thread_owner_user_id,
    d.created_at AS sent_at,
    'routine'::text AS charge,
    NULL::timestamptz AS edited_at,
    NULL::jsonb AS edit_history,
    d.content,
    NULL::text AS media_type,
    NULL::jsonb AS media_analysis,
    NULL::text AS bot_id,
    topics.primary_topic_id AS topic_id,
    NULL::uuid AS dyad_id,
    NULL::boolean AS thread_owner_partner_share,
    d.content AS canonical_text,
    to_tsvector('simple'::regconfig, COALESCE(d.content, '')) AS search_tsv,
    d.created_at AS sort_at,
    topics.primary_topic_id,
    COALESCE(topics.topic_ids, ARRAY[]::uuid[]) AS topic_ids,
    d.created_at AS source_created_at,
    COALESCE(d.updated_at, d.created_at) AS source_updated_at
FROM mediator.distillations d
LEFT JOIN LATERAL (
    SELECT
        (array_agg(at.topic_id ORDER BY at.topic_id))[1] AS primary_topic_id,
        array_agg(at.topic_id ORDER BY at.topic_id) AS topic_ids
    FROM mediator.artifact_topics at
    WHERE at.artifact_table = 'distillations'
      AND at.artifact_id = d.id
      AND at.status = 'active'
) topics ON true
WHERE d.status = 'active'
  AND COALESCE(d.visibility, 'private') = 'private'

UNION ALL

SELECT
    'artifact'::text AS source_type,
    ca.id AS source_id,
    NULL::uuid AS message_id,
    NULL::text AS direction,
    ca.user_id AS sender_id,
    NULL::uuid AS recipient_id,
    ca.user_id AS thread_owner_user_id,
    ca.created_at AS sent_at,
    'routine'::text AS charge,
    NULL::timestamptz AS edited_at,
    NULL::jsonb AS edit_history,
    artifact_text.canonical_text AS content,
    NULL::text AS media_type,
    jsonb_build_object(
        'artifact_type', ca.artifact_type,
        'payload_version', ca.payload_version,
        'revision_number', ca.revision_number,
        'conversation_id', ca.conversation_id,
        'created_by_turn_id', ca.created_by_turn_id,
        'expires_at', ca.expires_at
    ) AS media_analysis,
    ca.bot_id,
    c.topic_id,
    NULL::uuid AS dyad_id,
    NULL::boolean AS thread_owner_partner_share,
    artifact_text.canonical_text,
    to_tsvector('simple'::regconfig, COALESCE(artifact_text.canonical_text, '')) AS search_tsv,
    ca.created_at AS sort_at,
    c.topic_id AS primary_topic_id,
    CASE WHEN c.topic_id IS NULL THEN ARRAY[]::uuid[] ELSE ARRAY[c.topic_id] END AS topic_ids,
    ca.created_at AS source_created_at,
    ca.created_at AS source_updated_at
FROM mediator.conversation_artifacts ca
JOIN mediator.conversations c
  ON c.id = ca.conversation_id
CROSS JOIN LATERAL (
    SELECT CASE ca.artifact_type
        WHEN 'live_prep_brief' THEN btrim(concat_ws(E'\n',
            ca.payload#>>'{agenda,prep_summary}',
            ca.payload->>'notes',
            agenda_items.text
        ))
        WHEN 'live_debrief' THEN btrim(concat_ws(E'\n',
            ca.payload->>'review_summary',
            ca.payload#>>'{live_debrief,review_summary}',
            what_heard.text,
            what_decided.text,
            still_open.text,
            what_to_remember.text,
            durable_write_summary.text,
            open_questions.text
        ))
        WHEN 'review_summary' THEN btrim(concat_ws(E'\n',
            ca.payload->>'review_summary',
            ca.payload->>'summary',
            ca.payload#>>'{live_debrief,review_summary}',
            ca.payload#>>'{review,summary}'
        ))
        WHEN 'agenda_revision' THEN btrim(concat_ws(E'\n',
            ca.payload->>'prep_summary',
            ca.payload#>>'{agenda,prep_summary}',
            ca.payload->>'summary',
            ca.payload->>'notes',
            agenda_items.text,
            root_items.text
        ))
        WHEN 'transcript_reflection' THEN btrim(concat_ws(E'\n',
            transcript_reflection.text,
            ca.payload->>'summary',
            ca.payload->>'notes'
        ))
        ELSE btrim(concat_ws(E'\n',
            ca.payload->>'summary',
            ca.payload->>'title',
            ca.payload->>'notes',
            ca.payload->>'review_summary',
            ca.payload#>>'{live_debrief,review_summary}',
            ca.payload->>'prep_summary',
            ca.payload#>>'{agenda,prep_summary}',
            transcript_reflection.text,
            what_heard.text,
            what_decided.text,
            still_open.text,
            what_to_remember.text,
            durable_write_summary.text,
            open_questions.text,
            agenda_items.text,
            root_items.text
        ))
    END AS canonical_text
    FROM (SELECT CASE WHEN jsonb_typeof(ca.payload->'what_heard') = 'array' THEN (
            SELECT string_agg(value #>> '{}', E'\n' ORDER BY ord)
            FROM jsonb_array_elements(ca.payload->'what_heard') WITH ORDINALITY AS item(value, ord)
        ) ELSE ca.payload->>'what_heard' END AS text) what_heard
    CROSS JOIN (SELECT CASE WHEN jsonb_typeof(ca.payload->'what_decided') = 'array' THEN (
            SELECT string_agg(value #>> '{}', E'\n' ORDER BY ord)
            FROM jsonb_array_elements(ca.payload->'what_decided') WITH ORDINALITY AS item(value, ord)
        ) ELSE ca.payload->>'what_decided' END AS text) what_decided
    CROSS JOIN (SELECT CASE WHEN jsonb_typeof(ca.payload->'still_open') = 'array' THEN (
            SELECT string_agg(value #>> '{}', E'\n' ORDER BY ord)
            FROM jsonb_array_elements(ca.payload->'still_open') WITH ORDINALITY AS item(value, ord)
        ) ELSE ca.payload->>'still_open' END AS text) still_open
    CROSS JOIN (SELECT CASE WHEN jsonb_typeof(ca.payload->'what_to_remember') = 'array' THEN (
            SELECT string_agg(value #>> '{}', E'\n' ORDER BY ord)
            FROM jsonb_array_elements(ca.payload->'what_to_remember') WITH ORDINALITY AS item(value, ord)
        ) ELSE ca.payload->>'what_to_remember' END AS text) what_to_remember
    CROSS JOIN (SELECT CASE WHEN jsonb_typeof(ca.payload->'durable_write_summary') = 'array' THEN (
            SELECT string_agg(value #>> '{}', E'\n' ORDER BY ord)
            FROM jsonb_array_elements(ca.payload->'durable_write_summary') WITH ORDINALITY AS item(value, ord)
        ) ELSE ca.payload->>'durable_write_summary' END AS text) durable_write_summary
    CROSS JOIN (SELECT CASE WHEN jsonb_typeof(ca.payload->'open_questions') = 'array' THEN (
            SELECT string_agg(value #>> '{}', E'\n' ORDER BY ord)
            FROM jsonb_array_elements(ca.payload->'open_questions') WITH ORDINALITY AS item(value, ord)
        ) ELSE ca.payload->>'open_questions' END AS text) open_questions
    CROSS JOIN (SELECT CASE WHEN jsonb_typeof(ca.payload->'transcript_reflection') = 'array' THEN (
            SELECT string_agg(value #>> '{}', E'\n' ORDER BY ord)
            FROM jsonb_array_elements(ca.payload->'transcript_reflection') WITH ORDINALITY AS item(value, ord)
        ) ELSE ca.payload->>'transcript_reflection' END AS text) transcript_reflection
    CROSS JOIN (SELECT CASE WHEN jsonb_typeof(ca.payload#>'{agenda,items}') = 'array' THEN (
            SELECT string_agg(concat_ws(E'\n',
                value->>'title',
                value->>'intent',
                value->>'ask',
                value->>'done_when'
            ), E'\n' ORDER BY ord)
            FROM jsonb_array_elements(ca.payload#>'{agenda,items}') WITH ORDINALITY AS item(value, ord)
        ) END AS text) agenda_items
    CROSS JOIN (SELECT CASE WHEN jsonb_typeof(ca.payload->'items') = 'array' THEN (
            SELECT string_agg(concat_ws(E'\n',
                value->>'title',
                value->>'intent',
                value->>'ask',
                value->>'done_when'
            ), E'\n' ORDER BY ord)
            FROM jsonb_array_elements(ca.payload->'items') WITH ORDINALITY AS item(value, ord)
        ) END AS text) root_items
) artifact_text
WHERE ca.deleted_at IS NULL
  AND (ca.expires_at IS NULL OR ca.expires_at > now());

COMMENT ON VIEW mediator.v_searchable_content IS
    'Unified M1 retrieval read surface for messages, memories, observations, distillations, and artifacts. Excludes deleted/suppressed messages, inactive durable rows, deleted artifacts, and dyad_shareable non-message content.';

CREATE OR REPLACE VIEW mediator.v_searchable_messages AS
SELECT
    sc.message_id,
    sc.direction,
    sc.sender_id,
    sc.recipient_id,
    sc.thread_owner_user_id,
    sc.sent_at,
    sc.charge,
    sc.edited_at,
    sc.edit_history,
    sc.content,
    sc.media_type,
    sc.media_analysis,
    sc.bot_id,
    sc.topic_id,
    sc.dyad_id,
    sc.thread_owner_partner_share,
    sc.canonical_text,
    sc.search_tsv
FROM mediator.v_searchable_content sc
WHERE sc.source_type = 'message';

COMMENT ON VIEW mediator.v_searchable_messages IS
    'M1 compatibility view over mediator.v_searchable_content restricted to source_type=message.';

CREATE OR REPLACE FUNCTION mediator.cleanup_message_content_embedding()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    DELETE FROM mediator.content_embeddings
    WHERE source_type = 'message'
      AND source_id = OLD.id;

    UPDATE mediator.embed_jobs
    SET status = 'superseded',
        completed_at = COALESCE(completed_at, now()),
        updated_at = now(),
        last_error = COALESCE(last_error, 'message deleted; content embedding cleanup')
    WHERE source_type = 'message'
      AND source_id = OLD.id
      AND status IN ('pending','processing');

    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_messages_cleanup_content_embedding ON mediator.messages;
CREATE TRIGGER trg_messages_cleanup_content_embedding
AFTER DELETE ON mediator.messages
FOR EACH ROW
EXECUTE FUNCTION mediator.cleanup_message_content_embedding();

COMMIT;
