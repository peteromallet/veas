-- 0043_auth_magic_links: One-time Discord-DM magic-link auth (R5).
--
-- The Live Voice web app needs an authed user_id but DISCORD_CLIENT_ID/SECRET
-- aren't available, so OAuth code-grant isn't wireable.  Magic-link auth uses
-- the existing DISCORD_BOT_TOKEN_<bot_id> tokens: backend mints a 6-digit
-- code, DMs it to the user via the mediator bot, user enters it on the web,
-- backend mints a short-lived JWT.
--
-- This table stores the HMAC of each code (not the cleartext), tracks
-- attempts, and supports the per-discord-id rate limit at the application
-- layer. FORCE RLS + deny anon + service-role-only writes.

BEGIN;

CREATE TABLE mediator.auth_magic_links (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The web-app user this code was minted for. Resolved at request time
    -- via user_identities (transport='discord', address=$discord_id).
    user_id uuid NOT NULL REFERENCES mediator.users(id) ON DELETE CASCADE,
    discord_id text NOT NULL,
    -- HMAC-SHA256(code, secret); never store the cleartext code.
    code_hash bytea NOT NULL,
    -- Optional Discord channel id where the DM was sent (for forensics).
    dm_channel_id text,
    requested_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    attempts_used integer NOT NULL DEFAULT 0,
    consumed_at timestamptz,
    revoked_at timestamptz,

    CONSTRAINT auth_magic_links_max_attempts
        CHECK (attempts_used >= 0 AND attempts_used <= 10)
);

-- Look up the active code for a given discord_id quickly.  Partial because
-- consumed / revoked / expired rows aren't queried again.
CREATE INDEX idx_auth_magic_links_discord_active
    ON mediator.auth_magic_links (discord_id, requested_at DESC)
    WHERE consumed_at IS NULL AND revoked_at IS NULL;

CREATE INDEX idx_auth_magic_links_user_recent
    ON mediator.auth_magic_links (user_id, requested_at DESC);

ALTER TABLE mediator.auth_magic_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.auth_magic_links FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.auth_magic_links FROM anon;

DO $$
BEGIN
    CREATE POLICY deny_anon_auth_magic_links ON mediator.auth_magic_links
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Owner-scoped read (auth.uid() can read its own outstanding codes).
-- Writes happen via service-role only.
DO $$
BEGIN
    CREATE POLICY owner_scoped_auth_magic_links ON mediator.auth_magic_links
        FOR SELECT
        USING (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
