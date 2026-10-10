-- ============================================================================
-- FairSeat: waitlist entries and promotion tracking.
-- Additive only: references events, ticket_categories, users, ticket_bookings.
-- ============================================================================

CREATE TABLE ticket_waitlist_entries (
    id                   uuid PRIMARY KEY,
    event_id             uuid NOT NULL REFERENCES events (id) ON DELETE RESTRICT,
    ticket_category_id   uuid NOT NULL REFERENCES ticket_categories (id) ON DELETE RESTRICT,
    user_id              uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    quantity             integer NOT NULL CHECK (quantity > 0),
    status               text NOT NULL CHECK (status IN ('waiting', 'offered', 'converted', 'expired', 'cancelled')),
    position             bigserial,
    offered_at           timestamptz,
    offer_expires_at     timestamptz,
    promoted_booking_id  uuid REFERENCES ticket_bookings (id) ON DELETE SET NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ticket_waitlist_event_category_fk FOREIGN KEY (event_id, ticket_category_id)
        REFERENCES ticket_categories (event_id, id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX ticket_waitlist_active_user_uq ON ticket_waitlist_entries (event_id, ticket_category_id, user_id)
    WHERE status IN ('waiting', 'offered');

CREATE INDEX ticket_waitlist_queue_idx ON ticket_waitlist_entries (ticket_category_id, position)
    WHERE status = 'waiting';

CREATE INDEX ticket_waitlist_user_idx ON ticket_waitlist_entries (user_id, created_at DESC);
