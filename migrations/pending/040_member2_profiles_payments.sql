-- PROPOSAL (Member 2, Batch A). Local only until the team approves; run after 030-036.
-- Additive: new tables only. `users` is not altered: profile details live beside it.
--
--   user_profiles               details a member may edit about themselves
--   user_payment_profiles       one UPI id per person, encrypted at rest, masked for display
--   reimbursement_payouts       UPI payouts of approved expense claims, with every attempt kept
--   provider_webhook_receipts   one row per verified provider event, so a repeat is turned away

CREATE TABLE user_profiles (
    user_id        uuid PRIMARY KEY,
    club_id        uuid NOT NULL,
    display_name   text CHECK (display_name IS NULL OR char_length(btrim(display_name)) BETWEEN 1 AND 60),
    phone          text CHECK (phone IS NULL OR phone ~ '^\+?[0-9]{10,15}$'),
    department     text CHECK (department IS NULL OR char_length(btrim(department)) BETWEEN 1 AND 80),
    academic_year  smallint CHECK (academic_year IS NULL OR academic_year BETWEEN 1 AND 6),
    photo_url      text CHECK (photo_url IS NULL OR char_length(photo_url) <= 500),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT user_profiles_user_club_fk FOREIGN KEY (user_id, club_id)
        REFERENCES users (id, club_id) ON DELETE CASCADE
);

CREATE TABLE user_payment_profiles (
    user_id                  uuid PRIMARY KEY,
    club_id                  uuid NOT NULL,
    -- Fernet ciphertext of the normalised UPI id. The plain value is never stored or logged.
    upi_ciphertext           text NOT NULL,
    upi_masked               text NOT NULL CHECK (char_length(upi_masked) <= 80),
    -- unverified: format checked only. verified: the payout provider confirmed the account.
    verification_status      text NOT NULL DEFAULT 'unverified'
        CHECK (verification_status IN ('unverified', 'verified', 'invalid')),
    verification_provider    text,
    verification_reference   text,         -- the provider's fund account id
    registered_name          text,
    verified_at              timestamptz,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT user_payment_profiles_user_club_fk FOREIGN KEY (user_id, club_id)
        REFERENCES users (id, club_id) ON DELETE CASCADE,
    CONSTRAINT user_payment_profiles_verified_ck CHECK (
        (verification_status = 'verified') = (verified_at IS NOT NULL AND verification_provider IS NOT NULL))
);

CREATE TABLE reimbursement_payouts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id             uuid NOT NULL,
    expense_claim_id    uuid NOT NULL REFERENCES expense_claims (id) ON DELETE RESTRICT,
    payee_id            uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    initiated_by        uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    attempt             integer NOT NULL CHECK (attempt > 0),
    amount_paise        bigint NOT NULL CHECK (amount_paise > 0),
    currency            text NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),
    upi_masked          text NOT NULL,
    provider            text NOT NULL,
    -- initiated: claimed, provider not answered yet.  processing: provider accepted it.
    -- processed: provider confirmed success (only now is the claim paid).  failed / reversed: no money moved.
    status              text NOT NULL DEFAULT 'initiated'
        CHECK (status IN ('initiated', 'processing', 'processed', 'failed', 'reversed')),
    provider_payout_id  text UNIQUE,
    utr                 text,
    failure_reason      text,
    idempotency_key     text NOT NULL UNIQUE,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT reimbursement_payouts_attempt_uq UNIQUE (expense_claim_id, attempt)
);
-- At most one payout per claim that may still move money, or has.
CREATE UNIQUE INDEX reimbursement_payouts_one_live_uq ON reimbursement_payouts (expense_claim_id)
    WHERE status IN ('initiated', 'processing', 'processed');
CREATE INDEX reimbursement_payouts_club_idx ON reimbursement_payouts (club_id, created_at DESC);

CREATE TABLE provider_webhook_receipts (
    provider     text NOT NULL,
    event_key    text NOT NULL CHECK (char_length(event_key) BETWEEN 1 AND 255),
    event_type   text NOT NULL,
    outcome      text,
    received_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, event_key)
);
