BEGIN;

ALTER TABLE feedback ADD COLUMN IF NOT EXISTS resolution text NOT NULL DEFAULT 'open' CHECK (resolution IN ('open', 'resolved', 'ignored'));
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS resolved_at timestamptz;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS resolution_note text;

CREATE INDEX IF NOT EXISTS idx_feedback_open_created ON feedback (created_at DESC) WHERE resolution = 'open';

DO $$
BEGIN
    ALTER TABLE feedback ADD CONSTRAINT feedback_resolution_resolved_at_chk
        CHECK ((resolution = 'open' AND resolved_at IS NULL) OR (resolution <> 'open' AND resolved_at IS NOT NULL));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
