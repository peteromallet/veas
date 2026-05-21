-- 0055_live_product_statuses down: Restore the pre-0055 CHECK constraint,
-- DEFAULT, and partial indexes.

BEGIN;

-- ── Restore idx_conversations_spend_active to its pre-0055 predicate ───────
DROP INDEX IF EXISTS mediator.idx_conversations_spend_active;

CREATE INDEX idx_conversations_spend_active
    ON mediator.conversations (spend_usd_cents)
    WHERE status IN ('live', 'prepping', 'ready');

-- ── Restore idx_conversations_status_active to its pre-0055 predicate ──────
DROP INDEX IF EXISTS mediator.idx_conversations_status_active;

CREATE INDEX idx_conversations_status_active
    ON mediator.conversations (status)
    WHERE status IN ('prepping', 'ready', 'live', 'synthesizing', 'review_pending');

-- ── Restore DEFAULT 'prepping' ─────────────────────────────────────────────
ALTER TABLE mediator.conversations
    ALTER COLUMN status SET DEFAULT 'prepping';

-- ── Drop the expanded CHECK constraint ─────────────────────────────────────
ALTER TABLE mediator.conversations
    DROP CONSTRAINT IF EXISTS conversations_status_check;

-- ── Restore the CHECK to the 0053 shape (with debriefing/debrief_failed
--    but without preparing/active/completed) ─────────────────────────────────
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
        'prep_failed',
        'debriefing',
        'debrief_failed'
    ));

COMMIT;
