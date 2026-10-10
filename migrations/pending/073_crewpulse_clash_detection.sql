-- ============================================================================
-- 073_crewpulse_clash_detection.sql
-- Add validated duty scheduling fields to crew_tasks and an audit table for
-- authorized volunteer clash overrides.
-- ============================================================================

ALTER TABLE crew_tasks
    ADD COLUMN IF NOT EXISTS starts_at timestamptz,
    ADD COLUMN IF NOT EXISTS ends_at timestamptz;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'crew_tasks_schedule_check'
    ) THEN
        ALTER TABLE crew_tasks
            ADD CONSTRAINT crew_tasks_schedule_check
            CHECK (starts_at IS NULL OR ends_at IS NULL OR ends_at > starts_at);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS crew_tasks_schedule_idx
    ON crew_tasks (assignee_id, starts_at, ends_at)
    WHERE assignee_id IS NOT NULL;

-- Persistent audit trail of authorized volunteer clash overrides
CREATE TABLE IF NOT EXISTS crew_task_clash_overrides (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         uuid NOT NULL REFERENCES crew_tasks (id) ON DELETE CASCADE,
    event_id        uuid NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    club_id         uuid NOT NULL,
    volunteer_id    uuid NOT NULL,
    overridden_by   uuid NOT NULL,
    clash_summary   text NOT NULL,
    override_reason text NOT NULL CHECK (char_length(btrim(override_reason)) >= 5),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS crew_task_clash_overrides_task_idx
    ON crew_task_clash_overrides (task_id, created_at);

CREATE INDEX IF NOT EXISTS crew_task_clash_overrides_volunteer_idx
    ON crew_task_clash_overrides (volunteer_id, created_at);
