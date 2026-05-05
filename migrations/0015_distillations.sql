BEGIN;

-- Distillations are provisional synthesized explanations that connect multiple
-- concrete memories, grounded observations, themes, or source messages.
-- This migration intentionally does not backfill or mutate existing observations.
CREATE TABLE IF NOT EXISTS distillations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content text NOT NULL CHECK (length(btrim(content)) > 0),
    content_encrypted bytea,
    confidence text NOT NULL DEFAULT 'medium' CHECK (confidence IN ('high', 'medium', 'low')),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revised', 'retired', 'invalidated')),
    sensitivity text NOT NULL DEFAULT 'medium' CHECK (sensitivity IN ('low', 'medium', 'high')),
    visibility text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'dyad_shareable')),
    shareable_summary text,
    shareable_summary_encrypted bytea,
    source_user_ids uuid[] NOT NULL CHECK (cardinality(source_user_ids) > 0),
    related_memory_ids uuid[] NOT NULL DEFAULT '{}',
    related_observation_ids uuid[] NOT NULL DEFAULT '{}',
    related_theme_ids uuid[] NOT NULL DEFAULT '{}',
    supporting_message_ids uuid[] NOT NULL DEFAULT '{}',
    created_from_tool_call_id uuid REFERENCES tool_calls(id),
    triggering_message_id uuid REFERENCES messages(id),
    supersedes_distillation_id uuid REFERENCES distillations(id),
    superseded_by_distillation_id uuid REFERENCES distillations(id),
    revision_note text,
    revision_count integer NOT NULL DEFAULT 0 CHECK (revision_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    revised_at timestamptz,
    retired_at timestamptz,
    CHECK (
        cardinality(related_memory_ids) > 0
        OR cardinality(related_observation_ids) > 0
        OR cardinality(related_theme_ids) > 0
        OR cardinality(supporting_message_ids) > 0
    ),
    CHECK (
        visibility <> 'dyad_shareable'
        OR (shareable_summary IS NOT NULL AND length(btrim(shareable_summary)) > 0)
    ),
    CHECK (supersedes_distillation_id IS NULL OR supersedes_distillation_id <> id),
    CHECK (superseded_by_distillation_id IS NULL OR superseded_by_distillation_id <> id),
    CHECK (
        (status = 'revised' AND superseded_by_distillation_id IS NOT NULL AND revised_at IS NOT NULL)
        OR status <> 'revised'
    ),
    CHECK ((status = 'retired' AND retired_at IS NOT NULL) OR status <> 'retired')
);

CREATE INDEX IF NOT EXISTS idx_distillations_active_updated
    ON distillations (status, updated_at DESC)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_distillations_visibility_status_updated
    ON distillations (visibility, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_distillations_source_user_ids_gin
    ON distillations USING gin (source_user_ids);

CREATE INDEX IF NOT EXISTS idx_distillations_related_memory_ids_gin
    ON distillations USING gin (related_memory_ids);

CREATE INDEX IF NOT EXISTS idx_distillations_related_observation_ids_gin
    ON distillations USING gin (related_observation_ids);

CREATE INDEX IF NOT EXISTS idx_distillations_related_theme_ids_gin
    ON distillations USING gin (related_theme_ids);

CREATE INDEX IF NOT EXISTS idx_distillations_supporting_message_ids_gin
    ON distillations USING gin (supporting_message_ids);

CREATE INDEX IF NOT EXISTS idx_distillations_supersedes
    ON distillations (supersedes_distillation_id)
    WHERE supersedes_distillation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_distillations_superseded_by
    ON distillations (superseded_by_distillation_id)
    WHERE superseded_by_distillation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_distillations_triggering_message
    ON distillations (triggering_message_id)
    WHERE triggering_message_id IS NOT NULL;

REVOKE ALL ON TABLE distillations FROM anon;

ALTER TABLE distillations ENABLE ROW LEVEL SECURITY;
ALTER TABLE distillations FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    CREATE POLICY deny_anon_distillations ON distillations
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
