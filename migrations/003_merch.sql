-- MerchFlow: catalogue, variants with stock, orders and order lines.

CREATE TABLE merch_products (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id       uuid NOT NULL,
    name          text NOT NULL,
    description   text,
    base_price    numeric(12,2) NOT NULL CHECK (base_price > 0),
    member_price  numeric(12,2) NOT NULL CHECK (member_price > 0),
    image_url     text,
    is_active     boolean NOT NULL DEFAULT true,
    created_by    uuid NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT merch_member_price_not_above_base CHECK (member_price <= base_price)
);

CREATE TABLE product_variants (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id      uuid NOT NULL REFERENCES merch_products (id),
    size            text NOT NULL,
    color           text NOT NULL,
    sku             text NOT NULL UNIQUE,
    -- The CHECK is the last line of defence against overselling: a decrement
    -- that would go below zero fails instead of being stored.
    stock_quantity  integer NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
    price_override  numeric(12,2) CHECK (price_override > 0),
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX product_variants_product_idx ON product_variants (product_id);

CREATE TABLE merch_orders (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id              uuid NOT NULL,
    user_id              uuid NOT NULL,
    order_number         text NOT NULL UNIQUE,
    total_amount         numeric(12,2) NOT NULL CHECK (total_amount > 0),
    payment_status       text NOT NULL DEFAULT 'created'
        CHECK (payment_status IN ('created', 'pending', 'paid', 'failed', 'refunded')),
    status               text NOT NULL DEFAULT 'created'
        CHECK (status IN ('created', 'paid', 'packed', 'delivered', 'cancelled')),
    razorpay_order_id    text UNIQUE,
    razorpay_payment_id  text UNIQUE,
    -- Optional: merchandise sold for a particular event counts in that event's profit and loss.
    event_id             uuid,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX merch_orders_user_idx ON merch_orders (user_id, created_at DESC);
CREATE INDEX merch_orders_club_idx ON merch_orders (club_id, created_at DESC);

CREATE TABLE order_items (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id            uuid NOT NULL REFERENCES merch_orders (id),
    product_variant_id  uuid NOT NULL REFERENCES product_variants (id),
    quantity            integer NOT NULL CHECK (quantity > 0),
    unit_price          numeric(12,2) NOT NULL CHECK (unit_price > 0),
    line_total          numeric(12,2) NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX order_items_order_idx ON order_items (order_id);
