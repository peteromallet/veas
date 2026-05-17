-- 0047_v_bot_actions: Define mediator.v_bot_actions, the SQL view that
-- backs the ``get_bot_actions`` audit read tool.
--
-- Rationale (Project B work item 3)
-- ---------------------------------
-- Prior to this view, ``get_bot_actions`` materialised its denormalised
-- output via an application-layer query in app/services/tools/read_tools.py
-- that joined ``bot_turns`` to ``messages`` (twice — triggering + final
-- output), ``tool_calls`` and ``turn_audit_events``, then used GROUP BY
-- across the messages columns to collapse the tool_calls LEFT JOIN fan-out.
-- That shape produced the regression fixed in commit 221c700 — adding a
-- new column to the SELECT (e.g. ``tm.handling_result``) required updating
-- the GROUP BY list in lockstep, and missing one corrupted every audit row.
--
-- This view fixes the bug by construction: tool_calls and turn_audit_events
-- aggregation lives inside LATERAL subqueries, so the outer SELECT has no
-- GROUP BY at all. Adding a new column means adding it once, to the view.
--
-- Bot-scoping (Project B Settled Decision)
-- ----------------------------------------
-- ``bot_id`` (and ``topic_id``) are first-class columns of the view.
-- Callers MUST filter by bot_id explicitly — there is no opt-out flag.
-- The view exposes the column; the thin Python wrapper at
-- app/services/tools/read_tools.get_bot_actions enforces the WHERE clause.
--
-- Down migration: drops the view.

BEGIN;

CREATE OR REPLACE VIEW mediator.v_bot_actions AS
SELECT
    bt.id                          AS turn_id,
    bt.bot_id                      AS bot_id,
    bt.topic_id                    AS topic_id,
    bt.started_at                  AS started_at,
    bt.user_in_context             AS user_in_context,
    bt.triggered_by_message_id     AS triggered_by_message_id,
    bt.final_output_message_id     AS final_output_message_id,
    bt.failure_reason              AS failure_reason,
    COALESCE(bt.reasoning, '')     AS reasoning,
    tm.content                     AS triggering_content,
    tm.handling_result             AS triggering_handling_result,
    tm.processing_error            AS triggering_processing_error,
    tm.failure_class               AS triggering_failure_class,
    tm.next_retry_at               AS triggering_next_retry_at,
    om.content                     AS final_outbound_content,
    COALESCE(tc.tool_calls, '[]'::jsonb)       AS tool_calls,
    COALESCE(tae.audit_events, '[]'::jsonb)    AS audit_events
FROM mediator.bot_turns bt
LEFT JOIN mediator.messages tm
       ON tm.id = bt.triggered_by_message_id
LEFT JOIN mediator.messages om
       ON om.id = bt.final_output_message_id
LEFT JOIN LATERAL (
    SELECT jsonb_agg(to_jsonb(tcr) ORDER BY tcr.called_at) AS tool_calls
    FROM mediator.tool_calls tcr
    WHERE tcr.turn_id = bt.id
) tc ON TRUE
LEFT JOIN LATERAL (
    SELECT jsonb_agg(
             jsonb_build_object(
                 'id',          tae_row.id,
                 'turn_id',     tae_row.turn_id,
                 'event_seq',   tae_row.event_seq,
                 'event_type',  tae_row.event_type,
                 'step',        tae_row.step,
                 'severity',    tae_row.severity,
                 'occurred_at', tae_row.occurred_at,
                 'duration_ms', tae_row.duration_ms,
                 'actor',       tae_row.actor,
                 'message',     tae_row.message,
                 'metadata',    tae_row.metadata
             )
             ORDER BY tae_row.event_seq
           ) AS audit_events
    FROM mediator.turn_audit_events tae_row
    WHERE tae_row.turn_id = bt.id
) tae ON TRUE;

COMMENT ON VIEW mediator.v_bot_actions IS
    'Denormalised per-turn audit row used by get_bot_actions. '
    'Consumers MUST filter by bot_id; no opt-out scope flag exists. '
    'See migrations/0047_v_bot_actions.sql for rationale (Project B work item 3).';

COMMIT;
