-- ============================================================================
-- FairSeat event-side ticket configuration (Member 2). PROPOSED: needs team approval
-- before it is applied to any shared database.
--
-- Additive only: no existing table or column changes. Events without a row in
-- event_ticket_settings simply have no ticketing ("not configured").
--
-- Capacity model (strict partition): the event's ticket capacity is split exactly
-- across its categories. Bookings, holds, payments and tickets are NOT here; they
-- belong to the reservation service (Member 3) and reference these rows.
-- ============================================================================

-- Lets child tables prove their club_id matches the event's (composite FK below).
CREATE UNIQUE INDEX IF NOT EXISTS events_id_club_key ON events (id, club_id);

CREATE TABLE event_ticket_settings (
    event_id            uuid PRIMARY KEY,
    club_id             uuid NOT NULL,
    sales_enabled       boolean NOT NULL DEFAULT false,
    capacity            integer NOT NULL CHECK (capacity BETWEEN 1 AND 100000),
    opens_at            timestamptz NOT NULL,
    closes_at           timestamptz NOT NULL,
    max_per_booking     integer NOT NULL DEFAULT 10 CHECK (max_per_booking BETWEEN 1 AND 100),
    -- First time sales were live on a published event. Never cleared: from this moment
    -- (once opens_at has passed) bookings may exist, so structural edits are locked.
    sales_activated_at  timestamptz,
    created_by          uuid NOT NULL,
    updated_by          uuid NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT event_ticket_settings_event_fk
        FOREIGN KEY (event_id, club_id) REFERENCES events (id, club_id) ON DELETE CASCADE,
    CONSTRAINT event_ticket_settings_window_check CHECK (closes_at > opens_at),
    CONSTRAINT event_ticket_settings_event_club_key UNIQUE (event_id, club_id)
);

CREATE TABLE event_ticket_categories (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id                  uuid NOT NULL,
    club_id                   uuid NOT NULL,
    name                      text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 60),
    description               text CHECK (description IS NULL OR char_length(description) <= 300),
    -- Standard (non-member) price in integer paise. Member discounts are MemberCore's.
    price_paise               bigint NOT NULL CHECK (price_paise BETWEEN 0 AND 10000000),
    capacity                  integer NOT NULL CHECK (capacity BETWEEN 1 AND 100000),
    member_discount_eligible  boolean NOT NULL DEFAULT true,
    -- Hidden categories are paused, not deleted: they keep their seats and history.
    is_visible                boolean NOT NULL DEFAULT true,
    sort_order                smallint NOT NULL DEFAULT 0,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT event_ticket_categories_settings_fk
        FOREIGN KEY (event_id, club_id) REFERENCES event_ticket_settings (event_id, club_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX event_ticket_categories_name_key ON event_ticket_categories (event_id, lower(btrim(name)));
CREATE INDEX event_ticket_categories_event_idx ON event_ticket_categories (event_id, sort_order);
CREATE INDEX event_ticket_settings_club_idx ON event_ticket_settings (club_id) WHERE sales_enabled;

-- Backstop for the partition rule (the service checks it first, with readable errors):
-- categories never hold more seats than the event, and while sales are on they hold
-- exactly the event's capacity. Deferred, so a whole configuration can be replaced
-- in one transaction.
CREATE FUNCTION event_ticket_partition_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target    uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD.event_id ELSE NEW.event_id END;
    settings  record;
    allocated bigint;
    groups    integer;
BEGIN
    SELECT capacity, sales_enabled INTO settings FROM event_ticket_settings WHERE event_id = target;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT coalesce(sum(capacity), 0), count(*) INTO allocated, groups
        FROM event_ticket_categories WHERE event_id = target;
    IF allocated > settings.capacity
       OR (settings.sales_enabled AND (groups = 0 OR allocated <> settings.capacity)) THEN
        RAISE EXCEPTION 'Ticket categories (% seats) do not partition the event capacity (% seats)',
            allocated, settings.capacity
            USING ERRCODE = 'check_violation', CONSTRAINT = 'event_ticket_partition';
    END IF;
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER event_ticket_categories_partition
    AFTER INSERT OR UPDATE OR DELETE ON event_ticket_categories
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION event_ticket_partition_check();

CREATE CONSTRAINT TRIGGER event_ticket_settings_partition
    AFTER INSERT OR UPDATE ON event_ticket_settings
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION event_ticket_partition_check();
