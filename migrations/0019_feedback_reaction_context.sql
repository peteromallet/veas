BEGIN;

CREATE INDEX IF NOT EXISTS idx_feedback_reaction_user_created
    ON feedback (from_user_id, created_at DESC)
    WHERE source = 'reaction' AND target_type = 'message';

COMMIT;
