-- 0045_live_session_spend: per-session spend tally for budget guard.
--
-- Sprint 3 DoD: $2 soft / $4 hard per session. Soft cap triggers a UX
-- warning; hard cap refuses to spawn new bot turns. Stored as cents
-- (integer) to dodge float drift.

BEGIN;

ALTER TABLE mediator.conversations
    ADD COLUMN IF NOT EXISTS spend_usd_cents integer NOT NULL DEFAULT 0
        CHECK (spend_usd_cents >= 0);

CREATE INDEX IF NOT EXISTS idx_conversations_spend_active
    ON mediator.conversations (spend_usd_cents)
    WHERE status IN ('live', 'prepping', 'ready');

COMMIT;
