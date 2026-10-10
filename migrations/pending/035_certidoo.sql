-- CertiDoo: certificates anyone can check. The verify code is what the public page looks up;
-- a revoked certificate stays on record and says so.

CREATE TABLE certificates (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id            uuid NOT NULL REFERENCES clubs (id) ON DELETE RESTRICT,
    recipient_user_id  uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    event_id           uuid REFERENCES events (id) ON DELETE RESTRICT,
    kind               text NOT NULL CHECK (kind IN ('participation', 'volunteering', 'achievement')),
    title              text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200),
    note               text CHECK (note IS NULL OR length(note) <= 500),
    -- The recipient's name as printed, kept as issued even if the profile changes later.
    recipient_name     text NOT NULL,
    verify_code        text NOT NULL UNIQUE CHECK (verify_code ~ '^[A-Z2-7]{16}$'),
    issued_by          uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    issued_at          timestamptz NOT NULL DEFAULT now(),
    revoked_at         timestamptz,
    revoked_by         uuid REFERENCES users (id) ON DELETE RESTRICT,
    revoke_reason      text,
    CONSTRAINT certificates_revoked_ck CHECK (
        (revoked_at IS NULL AND revoked_by IS NULL AND revoke_reason IS NULL)
        OR (revoked_at IS NOT NULL AND revoked_by IS NOT NULL AND length(btrim(revoke_reason)) BETWEEN 1 AND 500)
    )
);

-- One live certificate of a kind per person per event; issuing again is a no-op, not a duplicate.
CREATE UNIQUE INDEX certificates_one_per_event_uq
    ON certificates (recipient_user_id, event_id, kind) WHERE revoked_at IS NULL AND event_id IS NOT NULL;
CREATE INDEX certificates_club_idx ON certificates (club_id, issued_at DESC);
CREATE INDEX certificates_recipient_idx ON certificates (recipient_user_id, issued_at DESC);
