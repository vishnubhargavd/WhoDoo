-- PROPOSAL (Member 2, Batch A). Local only until the team approves; needs 031 (memberships).
-- In-app membership renewal reminders. One row per membership, term and stage: the unique key
-- makes the reminder job safe to run any number of times, from any number of processes.
-- A reminder belongs to the term it was written for (term_expires_at). Renewing changes the
-- membership's expiry, so the old term's reminders stop showing without being deleted.

CREATE TABLE membership_renewal_reminders (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id          uuid NOT NULL,
    membership_id    uuid NOT NULL,
    user_id          uuid NOT NULL,
    plan_id          uuid NOT NULL,
    -- d<N>: N days before expiry.  expired: on or after expiry.
    stage            text NOT NULL CHECK (stage ~ '^(d[0-9]{1,3}|expired)$'),
    term_expires_at  timestamptz NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    read_at          timestamptz,
    CONSTRAINT membership_renewal_reminders_stage_uq UNIQUE (membership_id, term_expires_at, stage),
    CONSTRAINT membership_renewal_reminders_membership_fk FOREIGN KEY (membership_id, club_id)
        REFERENCES memberships (id, club_id) ON DELETE CASCADE,
    CONSTRAINT membership_renewal_reminders_user_fk FOREIGN KEY (user_id, club_id)
        REFERENCES users (id, club_id) ON DELETE CASCADE
);
CREATE INDEX membership_renewal_reminders_user_idx ON membership_renewal_reminders (user_id, created_at DESC);
