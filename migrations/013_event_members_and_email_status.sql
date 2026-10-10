-- Which club user a registration belongs to (when they were signed in), and an honest record
-- of what happened to each ticket email. Additive only.

ALTER TABLE event_registrations
    -- Set when the participant was signed in as a club user. A participant is not given tasks.
    ADD COLUMN IF NOT EXISTS user_id          uuid REFERENCES users (id) ON DELETE RESTRICT,
    -- sent: handed to the mail server.  saved: no mail server is set up, written to the outbox.
    -- failed: the mail server refused or could not be reached (also kept in the outbox).
    ADD COLUMN IF NOT EXISTS email_status     text;
ALTER TABLE event_registrations
    ADD CONSTRAINT event_registrations_email_status_ck CHECK (email_status IN ('sent', 'saved', 'failed'));

CREATE UNIQUE INDEX event_registrations_user_uq ON event_registrations (event_id, user_id)
    WHERE user_id IS NOT NULL AND status IN ('pending_payment', 'registered', 'checked_in');
