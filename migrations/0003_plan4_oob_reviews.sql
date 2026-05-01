BEGIN;

CREATE TABLE IF NOT EXISTS withheld_outbound_reviews (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    recipient_id uuid NOT NULL REFERENCES users(id),
    sender_id uuid REFERENCES users(id),
    outbound_id uuid REFERENCES messages(id),
    original_content text NOT NULL,
    suggested_rewrite text,
    reason text NOT NULL,
    verdict text NOT NULL CHECK (verdict IN ('rewrite', 'block')),
    checker_failed boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'resolved', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_withheld_outbound_reviews_status_created
    ON withheld_outbound_reviews (status, created_at);

CREATE INDEX IF NOT EXISTS idx_withheld_outbound_reviews_recipient
    ON withheld_outbound_reviews (recipient_id, created_at DESC);

COMMIT;
