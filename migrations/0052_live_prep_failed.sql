-- 0052_live_prep_failed: Add 'prep_failed' to mediator.conversations status CHECK.
--
-- Drops and re-adds the inline CHECK constraint on conversations.status with
-- 'prep_failed' added to the IN list.  Also creates a dedicated partial index
-- for quick lookup of prep_failed sessions (used by retry endpoints and
-- orphan-recovery sweeps).
--
-- The idx_conversations_status_active partial index is left unchanged:
-- 'prep_failed' is intentionally excluded from it because failed sessions
-- are not considered "active".

BEGIN;

-- ── Drop the existing inline CHECK on conversations.status ────────────────
ALTER TABLE mediator.conversations
    DROP CONSTRAINT IF EXISTS conversations_status_check;

-- ── Re-add the CHECK with 'prep_failed' in the expanded IN list ────────────
ALTER TABLE mediator.conversations
    ADD CONSTRAINT conversations_status_check
    CHECK (status IN (
        'prepping',
        'ready',
        'live',
        'ended',
        'synthesizing',
        'review_pending',
        'synthesized',
        'discarded',
        'failed',
        'prep_failed'
    ));

-- ── Partial index for prep_failed sessions (retry / orphan-recovery) ───────
CREATE INDEX IF NOT EXISTS idx_conversations_status_prep_failed
    ON mediator.conversations (status, created_at)
    WHERE status = 'prep_failed';

COMMIT;
