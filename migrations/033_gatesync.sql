-- GateSync: check-in at the door. A ticket (event_tickets, 020) is admitted at most once:
-- the primary key below is the ticket, so a second scan of the same QR code cannot insert.

CREATE TABLE ticket_redemptions (
    ticket_id        uuid PRIMARY KEY REFERENCES event_tickets (id) ON DELETE RESTRICT,
    event_id         uuid NOT NULL REFERENCES events (id) ON DELETE RESTRICT,
    club_id          uuid NOT NULL REFERENCES clubs (id) ON DELETE RESTRICT,
    scanned_by       uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    -- qr: the code was scanned.  manual: door staff admitted the holder from the guest list.
    method           text NOT NULL DEFAULT 'qr' CHECK (method IN ('qr', 'manual')),
    checked_in_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ticket_redemptions_event_idx ON ticket_redemptions (event_id, checked_in_at DESC);
