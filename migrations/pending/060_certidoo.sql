-- ============================================================================
-- CertiDoo: Verifiable Digital Certificates
--
-- Real attendance (GateSync) and completed volunteer contributions (CrewPulse)
-- back certificate eligibility. Unique verification references allow public
-- verification against server-owned records.
-- ============================================================================

CREATE TABLE certificates (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id              uuid NOT NULL,
    event_id             uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    recipient_user_id    uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    certificate_type     text NOT NULL CHECK (certificate_type IN ('participation', 'volunteer', 'achievement')),
    verification_code    varchar(64) NOT NULL UNIQUE,
    title                text NOT NULL CHECK (length(btrim(title)) BETWEEN 3 AND 200),
    issued_by            uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    issued_at            timestamptz NOT NULL DEFAULT now(),
    revoked_at           timestamptz,
    metadata_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT certificates_recipient_event_type_uq UNIQUE (event_id, recipient_user_id, certificate_type)
);

CREATE INDEX certificates_club_idx ON certificates (club_id, issued_at DESC);
CREATE INDEX certificates_recipient_idx ON certificates (recipient_user_id, issued_at DESC);
CREATE INDEX certificates_code_idx ON certificates (verification_code);
