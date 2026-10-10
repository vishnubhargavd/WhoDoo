-- Payments move from Razorpay to Stripe Checkout. The two provider ids on an order
-- become the Stripe Checkout Session and PaymentIntent, and webhook deliveries get
-- their own table so a repeated delivery is recognised and skipped.

ALTER TABLE merch_orders RENAME COLUMN razorpay_order_id TO stripe_checkout_session_id;
ALTER TABLE merch_orders RENAME COLUMN razorpay_payment_id TO stripe_payment_intent_id;
ALTER TABLE merch_orders
    RENAME CONSTRAINT merch_orders_razorpay_order_id_key TO merch_orders_stripe_checkout_session_id_key;
ALTER TABLE merch_orders
    RENAME CONSTRAINT merch_orders_razorpay_payment_id_key TO merch_orders_stripe_payment_intent_id_key;

ALTER TABLE merch_orders ADD COLUMN currency text NOT NULL DEFAULT 'INR';
-- A new order is waiting for payment from the moment it is placed.
ALTER TABLE merch_orders ALTER COLUMN payment_status SET DEFAULT 'pending';

CREATE TABLE payment_events (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider           text NOT NULL,
    -- Stripe may deliver the same event more than once; the second insert conflicts here.
    provider_event_id  text NOT NULL UNIQUE,
    event_type         text NOT NULL,
    merch_order_id     uuid REFERENCES merch_orders (id),
    payment_intent_id  text,
    outcome            text NOT NULL,
    detail             text,
    received_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX payment_events_order_idx ON payment_events (merch_order_id);
CREATE INDEX payment_events_intent_idx ON payment_events (payment_intent_id);
