-- ============================================================================
-- FairSeat Phase 4: ticket payments and refunds.
--
-- PENDING, like 020 and 021: not applied by `make migrate`; applied only to the
-- disposable test database by tests/conftest.py. Apply after 020_fairseat.sql and
-- 021_fairseat_event_settings.sql.
--
-- Additive: new columns on ticket_bookings (a FairSeat table), one rewritten
-- CHECK on it, and one new table. Nothing outside FairSeat is touched.
-- ============================================================================

ALTER TABLE ticket_bookings
    -- Provider ids. Unique, so one payment can never be attached to two bookings.
    ADD COLUMN stripe_checkout_session_id text UNIQUE,
    ADD COLUMN stripe_payment_intent_id   text UNIQUE,
    ADD COLUMN paid_at                    timestamptz,
    -- Money owed back to the purchaser. NULL when no refund is involved.
    --   required  : decided, not yet sent to the provider
    --   pending   : sent; the provider has not confirmed it
    --   succeeded : the provider confirmed it (only now is payment_status 'refunded')
    --   failed    : the provider refused or errored; a treasurer can retry
    ADD COLUMN refund_status              text
        CHECK (refund_status IN ('required', 'pending', 'succeeded', 'failed')),
    ADD COLUMN refund_reason              text,
    ADD COLUMN stripe_refund_id           text UNIQUE,
    ADD COLUMN refund_failure             text,
    -- How many times a refund has been sent to the provider. Each new attempt after a
    -- failure uses a fresh provider idempotency key; a repeat of the same attempt does not.
    ADD COLUMN refund_attempts            integer NOT NULL DEFAULT 0 CHECK (refund_attempts >= 0),
    ADD COLUMN refunded_at                timestamptz;

-- Which payment and refund states go with which booking states.
--   A paid booking that is cancelled, or whose payment arrived after its seats had
--   gone, stays 'paid' with a refund in progress until the provider confirms the
--   refund. Only then does it become 'refunded'.
ALTER TABLE ticket_bookings DROP CONSTRAINT ticket_bookings_state_ck;
-- refund_status is NULL when no refund is involved. A comparison with NULL is neither
-- true nor false and a CHECK lets that through, so it is read as 'none' here.
ALTER TABLE ticket_bookings ADD CONSTRAINT ticket_bookings_state_ck CHECK (
    CASE COALESCE(refund_status, 'none')
        WHEN 'none' THEN
               (status = 'pending_payment' AND payment_status IN ('pending', 'failed'))
            OR (status = 'confirmed' AND payment_status IN ('paid', 'not_required'))
            OR (status = 'expired' AND payment_status IN ('pending', 'failed'))
            OR (status = 'cancelled' AND payment_status IN ('pending', 'failed', 'not_required'))
        WHEN 'succeeded' THEN status IN ('expired', 'cancelled') AND payment_status = 'refunded'
        ELSE status IN ('expired', 'cancelled') AND payment_status = 'paid'
    END
);
ALTER TABLE ticket_bookings ADD CONSTRAINT ticket_bookings_paid_ck CHECK (
    (payment_status IN ('paid', 'refunded')) = (stripe_payment_intent_id IS NOT NULL AND paid_at IS NOT NULL)
);
ALTER TABLE ticket_bookings ADD CONSTRAINT ticket_bookings_refunded_ck CHECK (
    (COALESCE(refund_status, 'none') = 'succeeded') = (refunded_at IS NOT NULL)
);

CREATE INDEX ticket_bookings_refund_idx ON ticket_bookings (refund_status) WHERE refund_status IS NOT NULL;

-- One row per webhook delivery handled by the ticket endpoint. Separate from the
-- merchandise endpoint's payment_events, because Stripe sends every event to both.
CREATE TABLE ticket_payment_events (
    id                 uuid PRIMARY KEY,
    provider           text NOT NULL,
    provider_event_id  text NOT NULL UNIQUE,
    event_type         text NOT NULL,
    ticket_booking_id  uuid REFERENCES ticket_bookings (id) ON DELETE RESTRICT,
    payment_intent_id  text,
    outcome            text NOT NULL,
    detail             text,
    received_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ticket_payment_events_booking_idx ON ticket_payment_events (ticket_booking_id);
