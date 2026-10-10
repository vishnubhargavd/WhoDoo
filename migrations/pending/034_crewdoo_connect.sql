-- CrewDoo Connect: announcements that reach the right people, and who has read them.

CREATE TABLE announcements (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id       uuid NOT NULL REFERENCES clubs (id) ON DELETE RESTRICT,
    author_id     uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    title         text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 160),
    body          text NOT NULL CHECK (length(btrim(body)) BETWEEN 1 AND 5000),
    -- everyone: the whole club.  members: active members.  volunteers: volunteers and above.
    -- officers: event leads, treasurer, president.  event: people holding a ticket or a task for event_id.
    audience      text NOT NULL DEFAULT 'everyone'
        CHECK (audience IN ('everyone', 'members', 'volunteers', 'officers', 'event')),
    event_id      uuid REFERENCES events (id) ON DELETE RESTRICT,
    pinned        boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    removed_at    timestamptz,
    removed_by    uuid REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT announcements_event_audience_ck CHECK ((audience = 'event') = (event_id IS NOT NULL))
);

CREATE INDEX announcements_club_idx ON announcements (club_id, created_at DESC);

CREATE TABLE announcement_reads (
    announcement_id  uuid NOT NULL REFERENCES announcements (id) ON DELETE CASCADE,
    user_id          uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    read_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (announcement_id, user_id)
);
