-- 0059_content_embeddings_deferred_source_types.down: reverse 0059 by
-- restoring the 0058 source-type contract and searchable-content view.
--
-- Tightening the durable source-type checks requires removing any
-- ``conversation_note`` / ``theme`` rows from content_embeddings and embed_jobs
-- first so the legacy 0058 checks can be re-applied safely.

BEGIN;

DELETE FROM mediator.embed_jobs
WHERE source_type IN ('conversation_note', 'theme');

DELETE FROM mediator.content_embeddings
WHERE source_type IN ('conversation_note', 'theme');

ALTER TABLE mediator.content_embeddings
    DROP CONSTRAINT IF EXISTS content_embeddings_source_type_check,
    ADD CONSTRAINT content_embeddings_source_type_check
        CHECK (
            source_type IN (
                'message',
                'memory',
                'observation',
                'distillation',
                'artifact'
            )
        );

ALTER TABLE mediator.embed_jobs
    DROP CONSTRAINT IF EXISTS embed_jobs_source_type_check,
    ADD CONSTRAINT embed_jobs_source_type_check
        CHECK (
            source_type IN (
                'message',
                'memory',
                'observation',
                'distillation',
                'artifact'
            )
        );

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

COMMIT;
