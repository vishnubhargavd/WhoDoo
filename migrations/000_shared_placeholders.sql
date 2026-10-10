-- ============================================================================
-- TEMPORARY SHARED-TABLE PLACEHOLDERS
--
-- clubs, users and events belong to the member/auth and event modules
-- (Members 1 and 2). These minimal stand-ins exist only so the finance module
-- can run and be demoed on its own. Replace or reconcile them when the real
-- tables land; nothing in the finance tables has a foreign key to them, and
-- all code that reads them lives in app/services/shared_directory.py.
-- ============================================================================

CREATE TABLE IF NOT EXISTS clubs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id           uuid NOT NULL REFERENCES clubs (id),
    full_name         text NOT NULL,
    email             text NOT NULL UNIQUE,
    role              text NOT NULL DEFAULT 'member'
        CHECK (role IN ('president', 'treasurer', 'event_lead', 'volunteer', 'member')),
    is_active_member  boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id     uuid NOT NULL REFERENCES clubs (id),
    name        text NOT NULL,
    event_date  date,
    lead_id     uuid REFERENCES users (id),
    created_at  timestamptz NOT NULL DEFAULT now()
);
