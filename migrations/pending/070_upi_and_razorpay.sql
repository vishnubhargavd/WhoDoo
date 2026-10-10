-- ============================================================================
-- Razorpay Test Mode & Secure UPI Profiles (Task 1)
--
-- Enables authenticated users to store, mask, and manage their UPI profiles
-- for reimbursements, plus Razorpay order/payment tracking records.
-- ============================================================================

CREATE TABLE user_payment_profiles (
    user_id                     uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    upi_id                      text CHECK (upi_id IS NULL OR upi_id ~ '^[-a-zA-Z0-9._]{2,120}@[a-zA-Z]{2,64}$'),
    preferred_payout_method     text NOT NULL DEFAULT 'upi' CHECK (preferred_payout_method IN ('upi', 'bank_transfer', 'cash_manual')),
    bank_account_number_masked  text,
    bank_ifsc                   text,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE razorpay_orders (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id              uuid NOT NULL,
    user_id              uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    reference_type       text NOT NULL CHECK (reference_type IN ('membership_dues', 'ticket_booking', 'merch_order', 'club_bill')),
    reference_id         uuid NOT NULL,
    rzp_order_id         text NOT NULL UNIQUE,
    rzp_payment_id       text,
    rzp_signature        text,
    amount_paise         bigint NOT NULL CHECK (amount_paise > 0),
    currency             text NOT NULL DEFAULT 'INR',
    status               text NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'attempted', 'paid', 'failed')),
    metadata_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at           timestamptz NOT NULL DEFAULT now(),
    paid_at              timestamptz,
    failed_at            timestamptz
);

CREATE INDEX razorpay_orders_user_idx ON razorpay_orders (user_id, created_at DESC);
CREATE INDEX razorpay_orders_ref_idx ON razorpay_orders (reference_type, reference_id);
CREATE INDEX razorpay_orders_rzp_idx ON razorpay_orders (rzp_order_id);

CREATE TABLE reimbursement_payouts (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id             uuid NOT NULL REFERENCES expense_claims (id) ON DELETE RESTRICT,
    club_id              uuid NOT NULL,
    claimant_id          uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    amount_paise         bigint NOT NULL CHECK (amount_paise > 0),
    payout_mode          text NOT NULL CHECK (payout_mode IN ('upi', 'bank_transfer', 'manual')),
    destination_upi      text,
    provider             text NOT NULL DEFAULT 'manual' CHECK (provider IN ('razorpayx', 'manual')),
    provider_payout_id   text,
    status               text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'paid', 'failed')),
    initiated_by         uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    initiated_at         timestamptz NOT NULL DEFAULT now(),
    settled_at           timestamptz,
    notes                text
);

CREATE INDEX reimbursement_payouts_claim_idx ON reimbursement_payouts (claim_id);
