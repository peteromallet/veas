-- 0055_live_product_statuses: Extend mediator.conversations.status CHECK with
-- canonical statuses and update DEFAULT + partial indexes for the productization
-- sprint.  This migration is additive — legacy statuses are retained in the
-- CHECK constraint and index predicates.  No existing rows are rewritten;
-- backward-compatible reads are handled by the application layer.
--
-- Canonical statuses added:
--   preparing   (replaces 'prepping')
--   active      (replaces 'live')
--   completed   (replaces 'synthesized' / 'ended')
--
-- Legacy statuses retained:
--   prepping, live, ended, synthesizing, synthesized, discarded, failed
--
-- Previously canonical (from 0052/0053) retained:
--   ready, review_pending, prep_failed, debriefing, debrief_failed

BEGIN;

-- ── Drop the existing inline CHECK on conversations.status ────────────────
ALTER TABLE mediator.conversations
    DROP CONSTRAINT IF EXISTS conversations_status_check;

-- ── Re-add the CHECK with canonical statuses added ────────────────────────
ALTER TABLE mediator.conversations
    ADD CONSTRAINT conversations_status_check
    CHECK (status IN (
        -- Legacy statuses (retained for backward compatibility)
        'prepping',
        'live',
        'ended',
        'synthesizing',
        'synthesized',
        'discarded',
        'failed',
        -- Already canonical (from 0042 / 0052 / 0053)
        'ready',
        'review_pending',
        'prep_failed',
        'debriefing',
        'debrief_failed',
        -- New canonical statuses (this migration)
        'preparing',
        'active',
        'completed'
    ));

-- ── Change DEFAULT from legacy 'prepping' to canonical 'preparing' ────────
ALTER TABLE mediator.conversations
    ALTER COLUMN status SET DEFAULT 'preparing';

-- ── Update idx_conversations_status_active (from 0042) ────────────────────
-- Drop the old index…
DROP INDEX IF EXISTS mediator.idx_conversations_status_active;

-- …and recreate with canonical statuses added while retaining legacy values.
CREATE INDEX idx_conversations_status_active
    ON mediator.conversations (status)
    WHERE status IN (
        'prepping', 'ready', 'live', 'synthesizing', 'review_pending',
        'preparing', 'active'
    );

-- ── Update idx_conversations_spend_active (from 0045) ─────────────────────
-- Drop the old index…
DROP INDEX IF EXISTS mediator.idx_conversations_spend_active;

-- …and recreate with canonical statuses added while retaining legacy values.
CREATE INDEX idx_conversations_spend_active
    ON mediator.conversations (spend_usd_cents)
    WHERE status IN (
        'live', 'prepping', 'ready',
        'active', 'preparing'
    );

COMMIT;
