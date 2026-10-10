-- LegacyVault: handover notes one year's officers leave for the next. Each note belongs to
-- the role it is for; every saved version is kept, so nothing a predecessor wrote is lost.

CREATE TABLE handover_notes (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id       uuid NOT NULL REFERENCES clubs (id) ON DELETE RESTRICT,
    role          text NOT NULL CHECK (role IN ('president', 'treasurer', 'event_lead', 'volunteer', 'everyone')),
    title         text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 160),
    body          text NOT NULL CHECK (length(btrim(body)) BETWEEN 1 AND 20000),
    version       integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by    uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    updated_by    uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    archived_at   timestamptz
);

CREATE INDEX handover_notes_club_idx ON handover_notes (club_id, role, updated_at DESC);

CREATE TABLE handover_note_versions (
    note_id     uuid NOT NULL REFERENCES handover_notes (id) ON DELETE CASCADE,
    version     integer NOT NULL,
    title       text NOT NULL,
    body        text NOT NULL,
    saved_by    uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    saved_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (note_id, version)
);
