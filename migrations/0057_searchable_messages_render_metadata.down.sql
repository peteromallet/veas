-- 0057_searchable_messages_render_metadata.down: remove render metadata columns.

BEGIN;

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
    'M1 retrieval read surface. Production retrievers must read messages through this view so deleted/search-suppressed rows are excluded consistently.';

COMMIT;
