BEGIN;

-- Plan 8: data-layer security hardening.
--
-- Closes the gaps identified in the privacy audit:
--   1. RLS on `withheld_outbound_reviews` (created in 0003 without it).
--   2. FORCE ROW LEVEL SECURITY on every table with RLS so the table-owner role
--      cannot bypass policies. Service-role connections still get full access
--      because Supabase's service_role bypasses both RLS and FORCE RLS by
--      virtue of the BYPASSRLS attribute on that role.
--   3. pgcrypto-backed *_encrypted bytea columns alongside the most sensitive
--      plaintext fields. Plaintext columns are NOT dropped here; that is a
--      one-way step the operator must take in a follow-up migration after
--      verifying the backfill worked end to end.
--   4. Idempotent backfill DO-block. Reads the encryption key from a session
--      GUC (`app.encryption_key`) so the operator controls when it runs:
--          SET app.encryption_key = '<base64-32-byte-key>';
--          \i migrations/0007_security_hardening.sql
--      With no key set the backfill is skipped (NOTICE emitted) so the
--      migration stays safe to apply repeatedly without secrets in CI.
--
-- Note on numbering: this is filed as 0007 because 0006 is reserved for the
-- eval-results plan that is being authored in parallel. Logically this could
-- have been 0006_security_hardening.sql; the content is unchanged.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1. RLS for withheld_outbound_reviews (missing from 0003).
ALTER TABLE withheld_outbound_reviews ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    CREATE POLICY deny_anon_withheld_outbound_reviews ON withheld_outbound_reviews
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 2. FORCE RLS on every table that has RLS enabled. Idempotent; safe to re-run.
ALTER TABLE users FORCE ROW LEVEL SECURITY;
ALTER TABLE messages FORCE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE ROW LEVEL SECURITY;
ALTER TABLE themes FORCE ROW LEVEL SECURITY;
ALTER TABLE watch_items FORCE ROW LEVEL SECURITY;
ALTER TABLE observations FORCE ROW LEVEL SECURITY;
ALTER TABLE out_of_bounds FORCE ROW LEVEL SECURITY;
ALTER TABLE scheduled_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE bot_turns FORCE ROW LEVEL SECURITY;
ALTER TABLE tool_calls FORCE ROW LEVEL SECURITY;
ALTER TABLE feedback FORCE ROW LEVEL SECURITY;
ALTER TABLE llm_spend_log FORCE ROW LEVEL SECURITY;
ALTER TABLE system_state FORCE ROW LEVEL SECURITY;
ALTER TABLE withheld_outbound_reviews FORCE ROW LEVEL SECURITY;

-- 3. Encrypted columns. Use bytea so the application can store
-- AES-GCM ciphertext (nonce || tag || ct) opaquely. We keep the plaintext
-- columns in place; the application reads the encrypted column when it has a
-- value and falls back to plaintext otherwise.
ALTER TABLE out_of_bounds
    ADD COLUMN IF NOT EXISTS sensitive_core_encrypted bytea;

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS content_encrypted bytea;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS content_encrypted bytea;

ALTER TABLE bot_turns
    ADD COLUMN IF NOT EXISTS reasoning_encrypted bytea;

ALTER TABLE observations
    ADD COLUMN IF NOT EXISTS content_encrypted bytea;

-- 4. Idempotent backfill notice.
--
-- The runtime helpers in app/services/crypto.py use AES-GCM with a base64
-- 32-byte key (DATA_ENCRYPTION_KEY). pgcrypto cannot produce that exact
-- envelope from SQL, so the actual ciphertext for legacy rows is written by
-- a Python operator script (scripts/backfill_encryption.py), which is safe
-- to re-run because it only encrypts rows where *_encrypted IS NULL.
--
-- This DO-block exists so applying the migration emits a reminder rather
-- than silently leaving plaintext-only rows behind.
DO $$
BEGIN
    RAISE NOTICE 'Encrypted-column backfill is application-driven. Run: python -m scripts.backfill_encryption (with DATA_ENCRYPTION_KEY set) to populate *_encrypted columns. Re-runnable; only encrypts rows where the encrypted column is NULL. After verification, schedule a follow-up migration to DROP the plaintext columns.';
END $$;

COMMIT;
