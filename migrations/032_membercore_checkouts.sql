-- MemberCore dues payments. A dues row (031) is immutable, so the checkout session opened to
-- pay it lives here. The payment itself is proved by membership_dues_reconciliations.

CREATE TABLE membership_checkouts (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dues_id                   uuid NOT NULL,
    club_id                   uuid NOT NULL,
    user_id                   uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    provider                  text NOT NULL,
    checkout_session_id       text NOT NULL UNIQUE,
    payment_intent_id         text UNIQUE,
    -- open: waiting for the buyer.  paid: settled and recorded.  failed: declined.
    -- refund_due / refunded: money arrived for dues that could no longer be honoured.
    status                    text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'paid', 'failed', 'refund_due', 'refunded')),
    detail                    text,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT membership_checkouts_dues_fk FOREIGN KEY (dues_id, club_id)
        REFERENCES membership_dues (id, club_id) ON DELETE RESTRICT
);

CREATE INDEX membership_checkouts_dues_idx ON membership_checkouts (dues_id, created_at DESC);
