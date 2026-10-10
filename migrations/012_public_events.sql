-- Public event registration. Any student can register for a published event without an
-- account; the organiser chooses free or paid, a participant limit and a venue that fits.
-- Additive only: nothing existing is changed or removed.

CREATE TABLE venues (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL UNIQUE,
    kind        text NOT NULL,
    capacity    integer NOT NULL CHECK (capacity > 0),
    location    text,
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO venues (name, kind, capacity, location) VALUES
    ('Conference Room 204',     'Meeting room',   40,   'Admin Block, 2nd floor'),
    ('CS Lab 2',                'Computer lab',   60,   'Computer Science Block, 1st floor'),
    ('Seminar Hall B',          'Seminar hall',   120,  'Academic Block B, ground floor'),
    ('Seminar Hall A',          'Seminar hall',   150,  'Academic Block A, ground floor'),
    ('Library Discussion Hall', 'Hall',           200,  'Central Library, 3rd floor'),
    ('Mini Auditorium',         'Auditorium',     300,  'Student Activity Centre'),
    ('Indoor Sports Complex',   'Sports hall',    500,  'Sports Block'),
    ('Dr. APJ Abdul Kalam Auditorium', 'Auditorium', 800, 'Main Block'),
    ('Open Air Theatre',        'Outdoor',        1200, 'Behind the Main Block'),
    ('Central Lawn',            'Outdoor',        1500, 'Opposite the Main Gate');

-- `events.venue` (text) stays and holds the chosen venue's name, so schedule clash
-- detection keeps comparing venue names exactly as before.
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS venue_id          uuid REFERENCES venues (id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS registration_mode text NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS max_participants  integer,
    ADD COLUMN IF NOT EXISTS price_paise       bigint NOT NULL DEFAULT 0;

ALTER TABLE events
    ADD CONSTRAINT events_registration_mode_ck CHECK (registration_mode IN ('none', 'free', 'paid')),
    ADD CONSTRAINT events_max_participants_ck CHECK (max_participants IS NULL OR max_participants BETWEEN 1 AND 100000),
    ADD CONSTRAINT events_price_paise_ck CHECK (price_paise BETWEEN 0 AND 10000000),
    ADD CONSTRAINT events_registration_setup_ck CHECK (
        registration_mode = 'none'
        OR (max_participants IS NOT NULL AND (registration_mode = 'paid') = (price_paise > 0))
    );

-- One row per participant. Deleted some weeks after the event ends (REGISTRATION_RETENTION_DAYS).
CREATE TABLE event_registrations (
    id                   uuid PRIMARY KEY,
    event_id             uuid NOT NULL REFERENCES events (id) ON DELETE RESTRICT,
    club_id              uuid NOT NULL REFERENCES clubs (id) ON DELETE RESTRICT,
    -- 1, 2, 3 ... within the event, in the order people registered.
    participant_no       integer NOT NULL CHECK (participant_no > 0),
    -- What the participant is told, e.g. WD-7K3Q9F.
    participant_code     text NOT NULL UNIQUE,
    full_name            text NOT NULL CHECK (length(btrim(full_name)) BETWEEN 2 AND 120),
    phone                text NOT NULL,
    email                text NOT NULL,
    student_id           text NOT NULL,
    status               text NOT NULL
        CHECK (status IN ('pending_payment', 'registered', 'checked_in', 'cancelled', 'expired')),
    amount_paise         bigint NOT NULL CHECK (amount_paise >= 0),
    currency             text NOT NULL DEFAULT 'INR',
    hold_expires_at      timestamptz,
    checkout_session_id  text UNIQUE,
    payment_intent_id    text UNIQUE,
    paid_at              timestamptz,
    -- required: money arrived for a place that could not be given.  succeeded: sent back.
    refund_status        text CHECK (refund_status IN ('required', 'succeeded', 'failed')),
    -- SHA-256 of the QR credential and of the ticket link's token. Neither secret is stored.
    credential_digest    bytea NOT NULL UNIQUE CHECK (octet_length(credential_digest) = 32),
    access_digest        bytea NOT NULL UNIQUE CHECK (octet_length(access_digest) = 32),
    checked_in_at        timestamptz,
    checked_in_by        uuid REFERENCES users (id) ON DELETE RESTRICT,
    email_sent_at        timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT event_registrations_no_uq UNIQUE (event_id, participant_no),
    CONSTRAINT event_registrations_checked_in_ck CHECK ((status = 'checked_in') = (checked_in_at IS NOT NULL)),
    CONSTRAINT event_registrations_hold_ck CHECK ((status = 'pending_payment') = (hold_expires_at IS NOT NULL))
);

-- One place per person per event: by email and by student ID, among registrations still alive.
CREATE UNIQUE INDEX event_registrations_email_uq ON event_registrations (event_id, lower(email))
    WHERE status IN ('pending_payment', 'registered', 'checked_in');
CREATE UNIQUE INDEX event_registrations_student_uq ON event_registrations (event_id, upper(student_id))
    WHERE status IN ('pending_payment', 'registered', 'checked_in');
CREATE INDEX event_registrations_event_idx ON event_registrations (event_id, status);
