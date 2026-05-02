BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS cross_thread_sharing_default text;

DO $$
BEGIN
    ALTER TABLE users
        ADD CONSTRAINT users_cross_thread_sharing_default_check
        CHECK (cross_thread_sharing_default IS NULL OR cross_thread_sharing_default IN ('opt_in', 'opt_out'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
