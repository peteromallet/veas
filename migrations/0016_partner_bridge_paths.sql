BEGIN;

-- Historical bridge_candidates rows default to message_partner. This migration
-- does not auto-create or infer historical partner bridges; any reclassification
-- is manual or script-based.
ALTER TABLE bridge_candidates
    ADD COLUMN IF NOT EXISTS partner_path text NOT NULL DEFAULT 'message_partner';

ALTER TABLE bridge_candidates
    ALTER COLUMN partner_path SET DEFAULT 'message_partner';

UPDATE bridge_candidates
SET partner_path = 'message_partner'
WHERE partner_path IS NULL;

ALTER TABLE bridge_candidates
    ALTER COLUMN partner_path SET NOT NULL;

DO $$
BEGIN
    ALTER TABLE bridge_candidates
        ADD CONSTRAINT bridge_candidates_partner_path_check
        CHECK (partner_path IN (
            'message_partner',
            'coach_in_person',
            'casual_share',
            'hold_for_context',
            'ask_permission',
            'do_not_bridge'
        ));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_bridge_candidates_target_ready_message_partner
    ON bridge_candidates (target_user_id, created_at DESC)
    WHERE status = 'ready' AND partner_path = 'message_partner';

COMMIT;
