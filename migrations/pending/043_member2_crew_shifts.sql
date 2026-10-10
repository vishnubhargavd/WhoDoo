-- PROPOSAL (Member 2, Batch B). Local only until the team approves.
-- An optional time slot (shift) for a CrewPulse task, so overlapping duties can be found.
-- crew_tasks itself is not altered. Overrides of a flagged clash are recorded in audit_logs
-- and in the task's activity trail, with the organizer's reason.

CREATE TABLE crew_task_shifts (
    task_id    uuid PRIMARY KEY REFERENCES crew_tasks (id) ON DELETE CASCADE,
    club_id    uuid NOT NULL,
    starts_at  timestamptz NOT NULL,
    ends_at    timestamptz NOT NULL,
    CONSTRAINT crew_task_shifts_order_ck CHECK (ends_at > starts_at),
    CONSTRAINT crew_task_shifts_length_ck CHECK (ends_at - starts_at <= interval '7 days')
);
CREATE INDEX crew_task_shifts_club_time_idx ON crew_task_shifts (club_id, starts_at, ends_at);
