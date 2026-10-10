-- TrustLedger: the single immutable financial record for a club.

CREATE TABLE ledger_entries (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id          uuid NOT NULL,
    entry_type       text NOT NULL
        CHECK (entry_type IN ('income', 'expense', 'reimbursement', 'refund', 'adjustment')),
    source_module    text NOT NULL
        CHECK (source_module IN ('membership', 'event_ticket', 'merchandise',
                                 'expense_claim', 'fundraiser', 'manual')),
    reference_id     uuid,
    reference_type   text,
    amount           numeric(12,2) NOT NULL CHECK (amount > 0),
    currency         text NOT NULL DEFAULT 'INR',
    direction        text NOT NULL CHECK (direction IN ('credit', 'debit')),
    category         text,
    description      text,
    payer_id         uuid,
    payee_id         uuid,
    approved_by      uuid,
    payment_status   text NOT NULL
        CHECK (payment_status IN ('pending', 'approved', 'paid', 'rejected', 'refunded')),
    receipt_url      text,
    event_id         uuid,
    -- One key per real-world financial event, so a retried webhook or a
    -- double-clicked button cannot post the same entry twice.
    idempotency_key  text UNIQUE,
    created_by       uuid NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ledger_direction_matches_type CHECK (
        (entry_type = 'income' AND direction = 'credit')
        OR (entry_type IN ('expense', 'reimbursement', 'refund') AND direction = 'debit')
        OR entry_type = 'adjustment'
    )
);

CREATE INDEX ledger_entries_club_created_idx ON ledger_entries (club_id, created_at DESC);
CREATE INDEX ledger_entries_event_idx ON ledger_entries (event_id) WHERE event_id IS NOT NULL;
CREATE INDEX ledger_entries_reference_idx ON ledger_entries (source_module, reference_id);

-- Entries are immutable. Only payment_status may move (approved -> paid);
-- every other correction is a new 'adjustment' entry.
CREATE OR REPLACE FUNCTION ledger_entries_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ledger_entries are immutable: delete is not allowed'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (to_jsonb(NEW) - 'payment_status' - 'updated_at')
       IS DISTINCT FROM (to_jsonb(OLD) - 'payment_status' - 'updated_at') THEN
        RAISE EXCEPTION 'ledger_entries are immutable: only payment_status may change, post an adjustment entry instead'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER ledger_entries_immutable
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entries_guard();

-- Approval trail for expense claims. The foreign key to expense_claims is
-- added in 002_expenses.sql, where that table is created.
CREATE TABLE approval_logs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    expense_claim_id  uuid NOT NULL,
    approver_id       uuid NOT NULL,
    approver_role     text NOT NULL,
    action            text NOT NULL
        CHECK (action IN ('submitted', 'approved', 'rejected', 'change_requested', 'paid')),
    comment           text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX approval_logs_claim_idx ON approval_logs (expense_claim_id, created_at);

-- Audit trail for payment status changes, manual ledger entries and stock changes.
CREATE TABLE audit_logs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id      uuid NOT NULL,
    actor_id     uuid,
    action       text NOT NULL,
    entity_type  text NOT NULL,
    entity_id    uuid,
    details      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX audit_logs_entity_idx ON audit_logs (entity_type, entity_id);
