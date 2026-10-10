-- ============================================================================
-- CrewPulse (Member 2): volunteer tasks per event, with an activity trail.
--
-- Like the finance tables, user and club ids are plain UUIDs without foreign keys
-- to the placeholder users/clubs tables, so those can be reconciled later.
-- ============================================================================

CREATE TABLE crew_tasks (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id       uuid NOT NULL,
    event_id      uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    title         text NOT NULL CHECK (char_length(btrim(title)) BETWEEN 3 AND 120),
    description   text,
    assignee_id   uuid,
    priority      text NOT NULL DEFAULT 'medium' CHECK (priority IN ('low', 'medium', 'high')),
    is_critical   boolean NOT NULL DEFAULT false,
    status        text NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'in_progress', 'blocked', 'done')),
    blocker_note  text,
    due_at        timestamptz,
    created_by    uuid NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz,
    CONSTRAINT crew_tasks_completed_check CHECK ((status = 'done') = (completed_at IS NOT NULL)),
    CONSTRAINT crew_tasks_blocker_check CHECK (status <> 'blocked' OR blocker_note IS NOT NULL)
);

-- The same task cannot be added twice to one event (retries, double clicks).
CREATE UNIQUE INDEX crew_tasks_event_title_key ON crew_tasks (event_id, lower(btrim(title)));
CREATE INDEX crew_tasks_assignee_idx ON crew_tasks (assignee_id, status);

-- Who changed what on a task, and when.
CREATE TABLE crew_task_activity (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id     uuid NOT NULL,
    task_id     uuid NOT NULL REFERENCES crew_tasks (id) ON DELETE CASCADE,
    actor_id    uuid NOT NULL,
    action      text NOT NULL CHECK (action IN ('created', 'updated', 'assigned', 'status_changed')),
    from_value  text,
    to_value    text,
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX crew_task_activity_task_idx ON crew_task_activity (task_id, created_at);
