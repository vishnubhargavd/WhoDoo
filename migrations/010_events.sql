-- ============================================================================
-- Event Management (Member 2).
--
-- Extends the shared `events` table in place. Nothing is renamed or dropped, so
-- existing event ids and every event_id held by other modules keep working.
-- `name` stays the title column; `event_date` stays and is kept equal to the
-- start date in IST by the event service.
-- ============================================================================

ALTER TABLE events
    ADD COLUMN IF NOT EXISTS description text,
    ADD COLUMN IF NOT EXISTS starts_at   timestamptz,
    ADD COLUMN IF NOT EXISTS ends_at     timestamptz,
    ADD COLUMN IF NOT EXISTS venue       text,
    -- Events that existed before this migration were already in use: mark them published.
    ADD COLUMN IF NOT EXISTS status      text NOT NULL DEFAULT 'published',
    ADD COLUMN IF NOT EXISTS created_by  uuid,
    ADD COLUMN IF NOT EXISTS updated_at  timestamptz NOT NULL DEFAULT now();

ALTER TABLE events ALTER COLUMN status SET DEFAULT 'draft';

ALTER TABLE events DROP CONSTRAINT IF EXISTS events_status_check;
ALTER TABLE events ADD CONSTRAINT events_status_check
    CHECK (status IN ('draft', 'published', 'cancelled', 'completed'));

ALTER TABLE events DROP CONSTRAINT IF EXISTS events_time_order_check;
ALTER TABLE events ADD CONSTRAINT events_time_order_check
    CHECK (starts_at IS NULL OR ends_at IS NULL OR ends_at > starts_at);

CREATE INDEX IF NOT EXISTS events_club_starts_idx ON events (club_id, starts_at);

-- A double-clicked "Create" cannot make the same event twice.
CREATE UNIQUE INDEX IF NOT EXISTS events_club_name_start_key
    ON events (club_id, lower(btrim(name)), starts_at)
    WHERE status <> 'cancelled' AND starts_at IS NOT NULL;
