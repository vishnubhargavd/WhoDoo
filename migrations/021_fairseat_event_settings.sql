-- ============================================================================
-- FairSeat event side (Member 2) joined to the reservation tables (Member 3).
--
-- PENDING, like 020_fairseat.sql: not applied by `make migrate`; applied only to
-- the disposable test database by tests/conftest.py. Apply after 020.
--
-- This replaces Member 2's proposed 012_event_tickets.sql. His event_ticket_settings
-- table is kept as he wrote it. His event_ticket_categories table is NOT created:
-- there is one category table, ticket_categories from 020, which gains his category
-- fields here. Additive: one index on events, one new table, new columns and
-- constraints on a FairSeat table.
-- ============================================================================

-- Lets event_ticket_settings prove its club_id matches the event's (composite FK below).
CREATE UNIQUE INDEX IF NOT EXISTS events_id_club_key ON events (id, club_id);

-- One row per ticketed event. Events without a row have no ticketing.
CREATE TABLE event_ticket_settings (
    event_id            uuid PRIMARY KEY,
    club_id             uuid NOT NULL,
    sales_enabled       boolean NOT NULL DEFAULT false,
    -- The event's ticket capacity, split exactly across its categories (strict partition).
    capacity            integer NOT NULL CHECK (capacity BETWEEN 1 AND 100000),
    -- The event-wide booking window. Category windows can only narrow it.
    opens_at            timestamptz NOT NULL,
    closes_at           timestamptz NOT NULL,
    -- At most 6 tickets in one booking (the approved limit); an event may set fewer.
    max_per_booking     integer NOT NULL DEFAULT 6 CHECK (max_per_booking BETWEEN 1 AND 6),
    -- First time sales were live on a published event. Never cleared: from this moment
    -- (once opens_at has passed) bookings may exist, so structural edits are locked.
    sales_activated_at  timestamptz,
    created_by          uuid NOT NULL,
    updated_by          uuid NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT event_ticket_settings_event_fk
        FOREIGN KEY (event_id, club_id) REFERENCES events (id, club_id) ON DELETE CASCADE,
    CONSTRAINT event_ticket_settings_window_check CHECK (closes_at > opens_at)
);

CREATE INDEX event_ticket_settings_club_idx ON event_ticket_settings (club_id) WHERE sales_enabled;

-- Category fields from the event side, on the one category table.
--   active (020)             is "visible": a hidden category is paused, not deleted, and keeps its seats.
--   base_price_paise (020)   is the standard, non-member price.
ALTER TABLE ticket_categories
    ADD COLUMN description               text CHECK (description IS NULL OR char_length(description) <= 300),
    -- Whether MemberCore's member discount may apply to this category.
    ADD COLUMN member_discount_eligible  boolean NOT NULL DEFAULT true,
    ADD COLUMN sort_order                smallint NOT NULL DEFAULT 0,
    -- A category's own window is now optional. NULL means "the event's window".
    ALTER COLUMN sales_open_at  DROP NOT NULL,
    ALTER COLUMN sales_close_at DROP NOT NULL;

ALTER TABLE ticket_categories
    -- A ticket costs at most 10,000,000 paise (Rs 1,00,000). Names stay 1 to 120 characters (020).
    ADD CONSTRAINT ticket_categories_price_ceiling_ck CHECK (base_price_paise <= 10000000),
    ADD CONSTRAINT ticket_categories_window_pair_ck CHECK ((sales_open_at IS NULL) = (sales_close_at IS NULL)),
    -- A category belongs to a ticketed event. RESTRICT: settings cannot vanish under categories.
    ADD CONSTRAINT ticket_categories_settings_fk
        FOREIGN KEY (event_id) REFERENCES event_ticket_settings (event_id) ON DELETE RESTRICT;

CREATE INDEX ticket_categories_event_order_idx ON ticket_categories (event_id, sort_order);

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
        FROM ticket_categories WHERE event_id = target;
    IF allocated > settings.capacity
       OR (settings.sales_enabled AND (groups = 0 OR allocated <> settings.capacity)) THEN
        RAISE EXCEPTION 'Ticket categories (% seats) do not partition the event capacity (% seats)',
            allocated, settings.capacity
            USING ERRCODE = 'check_violation', CONSTRAINT = 'event_ticket_partition';
    END IF;
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER ticket_categories_partition
    AFTER INSERT OR UPDATE OR DELETE ON ticket_categories
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION event_ticket_partition_check();

CREATE CONSTRAINT TRIGGER event_ticket_settings_partition
    AFTER INSERT OR UPDATE ON event_ticket_settings
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION event_ticket_partition_check();
