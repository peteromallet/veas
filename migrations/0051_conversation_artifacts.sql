-- 0051_conversation_artifacts: Artifact + provenance layer for live voice sessions.
--
-- Adds two new tables under mediator schema and extends bot_turns with live-session
-- tracking columns.  The artifact layer makes live voice sessions first-class
-- agentic episodes by providing typed, immutable, revision-tracked outputs
-- (prep briefs, debriefs, summaries, etc.) plus provenance links back to the
-- conversation items and durable state tables that were inspected or created.
--
-- Sections:
--   1. mediator.conversation_artifacts — typed, revisioned artifact rows
--   2. mediator.artifact_links — provenance links to conversation + durable tables
--   3. mediator.bot_turns ALTER — conversation_id + kind columns for live turns
--   4. Partial indexes on bot_turns new columns
--   5. RLS: ENABLE + FORCE + REVOKE + deny_anon + owner-scoped policies
--
-- bot_turns.conversation_id and bot_turns.kind are nullable with no default, so
-- the existing _open_turn INSERT in agentic.py (which uses a hardcoded column
-- list) continues to work without modification.  No production path populates
-- these columns in Sprint 1 — they are wired by Sprint 2 (live prep) and
-- Sprint 3 (live debrief).
--
-- Allowed artifact_type values (stable format for parity test regex extraction):
--   live_prep_brief, live_debrief, review_summary, agenda_revision, transcript_reflection
--
-- Allowed relation values (original 0051 set; widened by 0054):
--   planned_item, summarized_from, evidence_quote, extracted_memory,
--   extracted_observation, extracted_distillation, created_commitment,
--   logged_event, created_follow_up, updated_topic_status
--   (+10 relations added in 0054: extracted_theme, updated_commitment,
--    closed_commitment, updated_follow_up, created_watch_item,
--    updated_watch_item, addressed_watch_item, created_oob, updated_oob,
--    lifted_oob)
--
-- Allowed target_table values (original 0051 set; widened by 0054):
--   conversations, conversation_items, transcript_turns, conversation_notes,
--   messages, memories, observations, distillations, commitments, events,
--   scheduled_jobs, topic_status
--   (+3 tables added in 0054: themes, watch_items, out_of_bounds)
--
-- UNIQUE constraint on (artifact_id, target_table, target_id, relation):
--   Created in this migration; dropped by 0054_artifact_links_widen_checks
--   to allow multiple evidence rows per artifact-target-relation tuple
--   (Sprint 4 insert-distinct semantics).
--
-- bot_turns is deliberately excluded from artifact_links.target_table:
--   provenance for the producing turn is stored via created_by_turn_id FK.
-- pregnancy_state is excluded because the table does not exist (pregnancy is
--   columns on mediator.users per 0032_pregnancy.sql).

BEGIN;

-- ===========================================================================
-- 1. mediator.conversation_artifacts — typed, revisioned artifact rows
-- ===========================================================================

CREATE TABLE mediator.conversation_artifacts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     uuid NOT NULL
        REFERENCES mediator.conversations(id) ON DELETE CASCADE,
    bot_id              text NOT NULL,
    user_id             uuid NOT NULL
        REFERENCES mediator.users(id),
    artifact_type       text NOT NULL
        CHECK (artifact_type IN ('live_prep_brief','live_debrief','review_summary','agenda_revision','transcript_reflection')),
    payload             jsonb NOT NULL,
    payload_version     integer NOT NULL DEFAULT 1
        CHECK (payload_version >= 1),
    revision_number     integer NOT NULL DEFAULT 1
        CHECK (revision_number >= 1),
    created_by_turn_id  uuid
        REFERENCES mediator.bot_turns(id) ON DELETE SET NULL,
    deleted_at          timestamptz,
    expires_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (conversation_id, artifact_type, revision_number)
);

-- Fast lookup of the latest revision for a given (conversation, artifact_type).
CREATE INDEX idx_conversation_artifacts_latest_rev
    ON mediator.conversation_artifacts (conversation_id, artifact_type, revision_number DESC);

-- Partial index for listing non-deleted artifacts for a conversation.
CREATE INDEX idx_conversation_artifacts_active
    ON mediator.conversation_artifacts (conversation_id)
    WHERE deleted_at IS NULL;

-- ===========================================================================
-- 2. mediator.artifact_links — provenance links
-- ===========================================================================

CREATE TABLE mediator.artifact_links (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id     uuid NOT NULL
        REFERENCES mediator.conversation_artifacts(id) ON DELETE CASCADE,
    target_table    text NOT NULL
        CHECK (target_table IN ('conversations','conversation_items','transcript_turns','conversation_notes','messages','memories','observations','distillations','commitments','events','scheduled_jobs','topic_status')),
    target_id       uuid NOT NULL,
    relation        text NOT NULL
        CHECK (relation IN ('planned_item','summarized_from','evidence_quote','extracted_memory','extracted_observation','extracted_distillation','created_commitment','logged_event','created_follow_up','updated_topic_status')),
    evidence        jsonb,
    deleted_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),

    UNIQUE (artifact_id, target_table, target_id, relation)
);

-- Reverse lookup: find all links pointing to a specific durable row.
CREATE INDEX idx_artifact_links_target
    ON mediator.artifact_links (target_table, target_id);

-- ===========================================================================
-- 3. mediator.bot_turns — live-session columns
-- ===========================================================================

ALTER TABLE mediator.bot_turns
    ADD COLUMN IF NOT EXISTS conversation_id uuid
        REFERENCES mediator.conversations(id) ON DELETE SET NULL;

ALTER TABLE mediator.bot_turns
    ADD COLUMN IF NOT EXISTS kind text
        CHECK (kind IS NULL OR kind IN ('live_prep','live_debrief'));

-- ===========================================================================
-- 4. Partial indexes on bot_turns new columns
-- ===========================================================================

CREATE INDEX IF NOT EXISTS idx_bot_turns_conversation_id
    ON mediator.bot_turns (conversation_id)
    WHERE conversation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_bot_turns_kind
    ON mediator.bot_turns (kind)
    WHERE kind IS NOT NULL;

-- ===========================================================================
-- 5. RLS — defense-in-depth, matching 0042 and 0038 conventions
-- ===========================================================================

-- conversation_artifacts ----------------------------------------------------

ALTER TABLE mediator.conversation_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.conversation_artifacts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.conversation_artifacts FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_conversation_artifacts ON mediator.conversation_artifacts
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_conversation_artifacts ON mediator.conversation_artifacts
        FOR ALL
        USING (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = conversation_artifacts.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ))
        WITH CHECK (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = conversation_artifacts.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- artifact_links ------------------------------------------------------------

ALTER TABLE mediator.artifact_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.artifact_links FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.artifact_links FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_artifact_links ON mediator.artifact_links
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_artifact_links ON mediator.artifact_links
        FOR ALL
        USING (EXISTS (
            SELECT 1
            FROM mediator.conversation_artifacts a
            JOIN mediator.conversations c ON c.id = a.conversation_id
            WHERE a.id = artifact_links.artifact_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ))
        WITH CHECK (EXISTS (
            SELECT 1
            FROM mediator.conversation_artifacts a
            JOIN mediator.conversations c ON c.id = a.conversation_id
            WHERE a.id = artifact_links.artifact_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
