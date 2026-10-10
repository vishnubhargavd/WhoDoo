-- WhoDoo Connect: Club Announcements and Notifications
CREATE TABLE IF NOT EXISTS public.club_announcements (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id         uuid NOT NULL,
    author_id       uuid NOT NULL,
    author_name     text NOT NULL,
    title           text NOT NULL,
    content         text NOT NULL,
    target_audience text NOT NULL DEFAULT 'all',
    is_pinned       boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS club_announcements_club_created_idx
    ON public.club_announcements (club_id, created_at DESC);
