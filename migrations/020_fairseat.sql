-- ============================================================================
-- FairSeat (Member 1): ticket categories, price quotes, bookings, idempotency
-- records and per-seat ticket rows.
--
-- PENDING. This file sits in migrations/pending/ so that `make migrate`,
-- `make seed` and `make run` do not apply it. It is applied only to the
-- disposable test database by tests/conftest.py. Move it up one directory, to
-- migrations/020_fairseat.sql, once the team approves it for a real database.
--
-- Additive only: five new tables. `events` and `users` are referenced, never
-- altered. Money is whole paise. Ids are supplied by the application.
-- ============================================================================

CREATE TABLE ticket_categories (
    id                uuid PRIMARY KEY,
    event_id          uuid NOT NULL REFERENCES events (id) ON DELETE RESTRICT,
    name              text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
    base_price_paise  bigint NOT NULL CHECK (base_price_paise >= 0),
    -- Bumped whenever the price changes, so a quote for the old price stops being accepted.
    pricing_version   integer NOT NULL DEFAULT 1 CHECK (pricing_version > 0),
    capacity          integer NOT NULL CHECK (capacity > 0),
    -- Sales are open from sales_open_at (inclusive) to sales_close_at (exclusive).
    sales_open_at     timestamptz NOT NULL,
    sales_close_at    timestamptz NOT NULL,
    active            boolean NOT NULL DEFAULT true,
    created_by        uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ticket_categories_sale_window_ck CHECK (sales_close_at > sales_open_at),
    CONSTRAINT ticket_categories_event_id_id_uq UNIQUE (event_id, id)
);

CREATE UNIQUE INDEX ticket_categories_event_name_key ON ticket_categories (event_id, lower(btrim(name)));

-- A price the server worked out for one purchaser, event, category and quantity.
-- Short-lived. The browser sends back only the id; the amounts are read from here.
CREATE TABLE ticket_quotes (
    id                     uuid PRIMARY KEY,
    purchaser_user_id      uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    event_id               uuid NOT NULL REFERENCES events (id) ON DELETE RESTRICT,
    ticket_category_id     uuid NOT NULL REFERENCES ticket_categories (id) ON DELETE RESTRICT,
    quantity               integer NOT NULL CHECK (quantity > 0),
    base_unit_price_paise  bigint NOT NULL CHECK (base_unit_price_paise >= 0),
    unit_price_paise       bigint NOT NULL CHECK (unit_price_paise >= 0),
    total_price_paise      bigint NOT NULL CHECK (total_price_paise >= 0),
    member_eligible        boolean NOT NULL,
    applied_discount_bps   integer NOT NULL CHECK (applied_discount_bps BETWEEN 0 AND 10000),
    discount_applied       boolean NOT NULL,
    discount_reason        text NOT NULL,
    discount_explanation   text NOT NULL,
    -- Which rules produced the price: the calculator's version and the category's price version.
    calculator_version     text NOT NULL,
    category_pricing_version integer NOT NULL,
    issued_at              timestamptz NOT NULL,
    expires_at             timestamptz NOT NULL,
    CONSTRAINT ticket_quotes_total_ck CHECK (total_price_paise = unit_price_paise * quantity),
    CONSTRAINT ticket_quotes_unit_ck CHECK (unit_price_paise <= base_unit_price_paise),
    CONSTRAINT ticket_quotes_window_ck CHECK (expires_at > issued_at),
    CONSTRAINT ticket_quotes_event_category_fk FOREIGN KEY (event_id, ticket_category_id)
        REFERENCES ticket_categories (event_id, id) ON DELETE RESTRICT
);

CREATE INDEX ticket_quotes_purchaser_idx ON ticket_quotes (purchaser_user_id, issued_at DESC);

CREATE TABLE ticket_bookings (
    id                    uuid PRIMARY KEY,
    event_id              uuid NOT NULL REFERENCES events (id) ON DELETE RESTRICT,
    ticket_category_id    uuid NOT NULL REFERENCES ticket_categories (id) ON DELETE RESTRICT,
    purchaser_user_id     uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    quote_id              uuid NOT NULL REFERENCES ticket_quotes (id) ON DELETE RESTRICT,
    quantity              integer NOT NULL CHECK (quantity > 0),
    unit_price_paise      bigint NOT NULL CHECK (unit_price_paise >= 0),
    total_price_paise     bigint NOT NULL CHECK (total_price_paise >= 0),
    currency              text NOT NULL DEFAULT 'INR',
    member_eligible       boolean NOT NULL,
    applied_discount_bps  integer NOT NULL CHECK (applied_discount_bps BETWEEN 0 AND 10000),
    discount_applied      boolean NOT NULL,
    discount_reason       text NOT NULL,
    discount_explanation  text NOT NULL,
    status                text NOT NULL CHECK (status IN ('pending_payment', 'confirmed', 'cancelled', 'expired')),
    payment_status        text NOT NULL CHECK (payment_status IN ('pending', 'failed', 'paid', 'not_required', 'refunded')),
    hold_expires_at       timestamptz,
    cancelled_at          timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ticket_bookings_total_ck CHECK (total_price_paise = unit_price_paise * quantity),
    -- Only a booking that is waiting for payment holds seats until a deadline.
    CONSTRAINT ticket_bookings_hold_ck CHECK ((status = 'pending_payment') = (hold_expires_at IS NOT NULL)),
    -- Which payment states go with which booking states. A declined payment ('failed')
    -- leaves the hold in place so the buyer can try again before it expires.
    CONSTRAINT ticket_bookings_state_ck CHECK (
           (status = 'pending_payment' AND payment_status IN ('pending', 'failed'))
        OR (status = 'confirmed'       AND payment_status IN ('paid', 'not_required'))
        OR (status = 'expired'         AND payment_status IN ('pending', 'failed'))
        OR (status = 'cancelled'       AND payment_status IN ('pending', 'failed', 'not_required', 'refunded'))
    ),
    -- Free bookings need no payment; anything with a price does.
    CONSTRAINT ticket_bookings_free_ck CHECK ((payment_status = 'not_required') = (total_price_paise = 0)),
    CONSTRAINT ticket_bookings_event_category_fk FOREIGN KEY (event_id, ticket_category_id)
        REFERENCES ticket_categories (event_id, id) ON DELETE RESTRICT
);

-- Seats in use are counted per category, and per purchaser within an event.
CREATE INDEX ticket_bookings_capacity_idx ON ticket_bookings (ticket_category_id, status, hold_expires_at);
CREATE INDEX ticket_bookings_purchaser_idx ON ticket_bookings (purchaser_user_id, event_id, created_at DESC);
CREATE INDEX ticket_bookings_expiry_idx ON ticket_bookings (hold_expires_at) WHERE status = 'pending_payment';

-- One row per booking request. The unique key makes a retried request return the
-- booking it made the first time instead of making another.
CREATE TABLE ticket_booking_commands (
    id                   uuid PRIMARY KEY,
    purchaser_user_id    uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    event_id             uuid NOT NULL REFERENCES events (id) ON DELETE RESTRICT,
    idempotency_key      varchar(128) NOT NULL,
    request_fingerprint  char(64) NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    booking_id           uuid NOT NULL REFERENCES ticket_bookings (id) ON DELETE RESTRICT,
    created_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ticket_booking_commands_idem_uq UNIQUE (purchaser_user_id, event_id, idempotency_key)
);

-- One row per seat of a confirmed booking: the entitlement GateSync will later turn
-- into a QR credential. No credential is issued in this phase (digest stays NULL).
CREATE TABLE event_tickets (
    id                 uuid PRIMARY KEY,
    booking_id         uuid NOT NULL REFERENCES ticket_bookings (id) ON DELETE RESTRICT,
    event_id           uuid NOT NULL REFERENCES events (id) ON DELETE RESTRICT,
    holder_user_id     uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    seat_number        integer NOT NULL CHECK (seat_number > 0),
    credential_digest  bytea UNIQUE,
    status             text NOT NULL CHECK (status IN ('pending_issue', 'issued', 'revoked', 'checked_in')),
    issued_at          timestamptz,
    checked_in_at      timestamptz,
    revoked_at         timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT event_tickets_credential_digest_ck CHECK (
        credential_digest IS NULL OR octet_length(credential_digest) = 32
    ),
    CONSTRAINT event_tickets_revoked_ck CHECK ((status = 'revoked') = (revoked_at IS NOT NULL)),
    CONSTRAINT event_tickets_seat_uq UNIQUE (booking_id, seat_number)
);

CREATE INDEX event_tickets_event_idx ON event_tickets (event_id, status);
