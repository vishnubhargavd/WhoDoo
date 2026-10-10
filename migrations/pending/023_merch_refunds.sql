-- PROPOSED: apply after 004 and review together with the merchandise refund service.
-- The plain-SQL runner owns the transaction. No provider call occurs in a DB transaction.
ALTER TABLE merch_orders
    ADD COLUMN refund_status text CHECK (refund_status IN ('required', 'pending', 'failed', 'succeeded')),
    ADD COLUMN stripe_refund_id text,
    ADD COLUMN refund_reason text,
    ADD COLUMN refund_failure text,
    ADD COLUMN refund_attempts integer NOT NULL DEFAULT 0 CHECK (refund_attempts >= 0);

CREATE UNIQUE INDEX merch_orders_refund_id_key ON merch_orders(stripe_refund_id)
    WHERE stripe_refund_id IS NOT NULL;
CREATE INDEX merch_orders_open_refunds_idx ON merch_orders(club_id, refund_status)
    WHERE refund_status IN ('required', 'pending', 'failed');
