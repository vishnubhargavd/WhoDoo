-- Snap-to-Reimburse: receipt uploads, expense claims and their approval trail.

CREATE TABLE expense_claims (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id              uuid NOT NULL,
    submitted_by         uuid NOT NULL,
    event_id             uuid,
    title                text NOT NULL,
    description          text,
    category             text NOT NULL
        CHECK (category IN ('supplies', 'food', 'travel', 'printing', 'decoration', 'other')),
    amount               numeric(12,2) NOT NULL CHECK (amount > 0),
    expense_date         date NOT NULL,
    vendor_name          text,
    receipt_url          text NOT NULL,
    ocr_raw_response     jsonb,
    ocr_confidence       numeric(5,2),
    extracted_amount     numeric(12,2),
    extracted_vendor     text,
    extracted_date       date,
    status               text NOT NULL
        CHECK (status IN ('draft', 'pending_approval', 'approved', 'rejected', 'change_requested', 'paid')),
    -- 'first' = waiting for the event lead (or a stand-in); 'final' = waiting for
    -- the treasurer or president on a claim at or above the approval limit.
    approval_stage       text NOT NULL DEFAULT 'first' CHECK (approval_stage IN ('first', 'final')),
    first_approved_by    uuid,
    current_approver_id  uuid,
    final_approver_id    uuid,
    rejection_reason     text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX expense_claims_submitter_idx ON expense_claims (submitted_by, created_at DESC);
CREATE INDEX expense_claims_queue_idx ON expense_claims (club_id, status);

-- A receipt is uploaded and read by OCR before the claim exists. Keeping the OCR
-- result server-side means the claim's extracted_* fields never come from the browser.
CREATE TABLE receipt_uploads (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id           uuid NOT NULL,
    uploaded_by       uuid NOT NULL,
    receipt_url       text NOT NULL,
    content_type      text NOT NULL,
    ocr_raw_response  jsonb,
    ocr_confidence    numeric(5,2) NOT NULL DEFAULT 0,
    extracted_amount  numeric(12,2),
    extracted_vendor  text,
    extracted_date    date,
    expense_claim_id  uuid REFERENCES expense_claims (id),
    created_at        timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE approval_logs
    ADD CONSTRAINT approval_logs_claim_fk FOREIGN KEY (expense_claim_id) REFERENCES expense_claims (id);
