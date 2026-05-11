BEGIN;

-- ============================================================
-- Sprint 1: Foundation schema — topics, bots, bindings, channels, identities
-- ============================================================

-- 1. Topics — the central organizing concept for all artifacts
CREATE TABLE IF NOT EXISTS topics (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text NOT NULL UNIQUE,
    display_name text NOT NULL,
    description text NOT NULL DEFAULT '',
    participants_shape text NOT NULL DEFAULT 'dyad' CHECK (participants_shape IN ('dyad', 'solo', 'group')),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- 2. Bots — intentionally thin; scope/version fields live in code per §3
CREATE TABLE IF NOT EXISTS bots (
    id text PRIMARY KEY,
    display_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- 3. Dyads — a single row per relationship pair
CREATE TABLE IF NOT EXISTS dyads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- 4. Dyad members — links users to their dyad
CREATE TABLE IF NOT EXISTS dyad_members (
    dyad_id uuid NOT NULL REFERENCES dyads(id),
    user_id uuid NOT NULL REFERENCES users(id),
    joined_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dyad_id, user_id)
);

-- 5. Bot bindings — which bots are bound to which dyads/users
-- XOR: exactly one of dyad_id or user_id must be non-null
CREATE TABLE IF NOT EXISTS bot_bindings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    bot_id text NOT NULL REFERENCES bots(id),
    dyad_id uuid REFERENCES dyads(id),
    user_id uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT bot_bindings_xor CHECK ((user_id IS NOT NULL) <> (dyad_id IS NOT NULL))
);

-- 6. Channels — transport-specific addresses where bots listen
-- Two uniqueness mechanisms:
--   (a) raw-column UNIQUE for standard direct-match lookups
--   (b) expression index for COALESCE'd ON CONFLICT used by seed_channels.py
CREATE TABLE IF NOT EXISTS channels (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    bot_id text NOT NULL REFERENCES bots(id),
    transport text NOT NULL CHECK (transport IN ('discord', 'whatsapp', 'sms', 'web')),
    address text NOT NULL,
    guild_id text,
    channel_id text,
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (transport, address, guild_id, channel_id)
);

-- Expression-based unique index: COALESCE treats NULL guild_id/channel_id as empty string,
-- allowing seed_channels.py to use ON CONFLICT (transport, address, COALESCE(guild_id,''), COALESCE(channel_id,''))
-- Without this index, the expression-based ON CONFLICT target would fail with
-- 'no unique or exclusion constraint matching the on conflict specification'.
CREATE UNIQUE INDEX IF NOT EXISTS channels_uniq_coalesced
    ON channels (transport, address, COALESCE(guild_id, ''), COALESCE(channel_id, ''));

-- 7. User identities — maps transport+address pairs to user records
-- verified_at is nullable; account-linking workflows fill it in S4+
CREATE TABLE IF NOT EXISTS user_identities (
    transport text NOT NULL CHECK (transport IN ('discord', 'whatsapp', 'sms', 'legacy')),
    address text NOT NULL,
    user_id uuid NOT NULL REFERENCES users(id),
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (transport, address)
);

-- ============================================================
-- Seed data
-- ============================================================

-- Seed the 'relationship' topic — the default scope for the mediator
INSERT INTO topics (slug, display_name, description, participants_shape)
VALUES ('relationship', 'Relationship', 'Default relationship mediation topic', 'dyad')
ON CONFLICT (slug) DO NOTHING;

-- Seed the mediator bot
INSERT INTO bots (id, display_name)
VALUES ('mediator', 'Mediator')
ON CONFLICT (id) DO NOTHING;

-- Seed a single dyad and link it to the existing users.
-- Uses an existence-check DO block rather than ON CONFLICT on a random PK
-- so that re-running the migration is a true no-op.
DO $$
DECLARE
    _dyad_id uuid;
    _user_a_id uuid;
    _user_b_id uuid;
BEGIN
    -- Check if any dyad already has a binding for mediator
    PERFORM 1 FROM bot_bindings WHERE bot_id = 'mediator' AND dyad_id IS NOT NULL;
    IF FOUND THEN
        RETURN;
    END IF;

    -- Find two users to form the dyad
    SELECT id INTO _user_a_id FROM users ORDER BY created_at LIMIT 1;
    SELECT id INTO _user_b_id FROM users WHERE id <> _user_a_id ORDER BY created_at LIMIT 1;

    IF _user_a_id IS NULL OR _user_b_id IS NULL THEN
        -- Not enough users — skip dyad creation; seed_channels.sql populates identities
        -- and dyad can be formed later
        RETURN;
    END IF;

    -- Create dyad
    INSERT INTO dyads DEFAULT VALUES RETURNING id INTO _dyad_id;

    -- Add members
    INSERT INTO dyad_members (dyad_id, user_id) VALUES (_dyad_id, _user_a_id);
    INSERT INTO dyad_members (dyad_id, user_id) VALUES (_dyad_id, _user_b_id);

    -- Create bot binding for mediator to this dyad
    INSERT INTO bot_bindings (bot_id, dyad_id) VALUES ('mediator', _dyad_id);
END $$;

-- ============================================================
-- Backfill user_identities from users.phone
-- transport='legacy' per locked decision §16.8 (all existing rows).
-- ============================================================
INSERT INTO user_identities (transport, address, user_id)
SELECT 'legacy', phone, id
FROM users
WHERE phone IS NOT NULL
ON CONFLICT (transport, address) DO NOTHING;

-- ============================================================
-- Indexes
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_bot_bindings_bot_dyad ON bot_bindings (bot_id, dyad_id) WHERE dyad_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bot_bindings_bot_user ON bot_bindings (bot_id, user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_channels_bot_transport ON channels (bot_id, transport);
CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities (user_id);
CREATE INDEX IF NOT EXISTS idx_dyad_members_user ON dyad_members (user_id);

-- ============================================================
-- RLS: deny anon on all new tables
-- ============================================================

ALTER TABLE topics ENABLE ROW LEVEL SECURITY;
ALTER TABLE topics FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE topics FROM anon;

ALTER TABLE bots ENABLE ROW LEVEL SECURITY;
ALTER TABLE bots FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE bots FROM anon;

ALTER TABLE dyads ENABLE ROW LEVEL SECURITY;
ALTER TABLE dyads FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE dyads FROM anon;

ALTER TABLE dyad_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE dyad_members FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE dyad_members FROM anon;

ALTER TABLE bot_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_bindings FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE bot_bindings FROM anon;

ALTER TABLE channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE channels FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE channels FROM anon;

ALTER TABLE user_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_identities FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE user_identities FROM anon;

-- Deny policies (idempotent via DO block with duplicate_object catch)
DO $$
BEGIN
    CREATE POLICY deny_anon_topics ON topics FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_bots ON bots FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_dyads ON dyads FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_dyad_members ON dyad_members FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_bot_bindings ON bot_bindings FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_channels ON channels FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_user_identities ON user_identities FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;