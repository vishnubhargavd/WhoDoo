-- PROPOSAL (Member 2, Batch A). Local only until the team approves.
-- An event's budget, in whole paise. Spending against it is read from TrustLedger
-- (ledger_entries.event_id); nothing here duplicates those amounts.

CREATE TABLE event_budgets (
    event_id      uuid PRIMARY KEY,
    club_id       uuid NOT NULL,
    budget_paise  bigint NOT NULL CHECK (budget_paise BETWEEN 0 AND 999999999999),
    updated_by    uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT event_budgets_event_fk FOREIGN KEY (event_id, club_id)
        REFERENCES events (id, club_id) ON DELETE CASCADE
);
