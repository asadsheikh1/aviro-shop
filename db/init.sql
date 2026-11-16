-- AVIRO schema + seed data. Runs once when the Postgres volume is first created.
-- Each service only touches its own tables.

-- inventory
CREATE TABLE products (
    sku          text PRIMARY KEY,
    name         text NOT NULL,
    price_cents  integer NOT NULL CHECK (price_cents >= 0),
    pack_size    integer NOT NULL DEFAULT 1,   -- units per pack; price is per pack
    stock        integer NOT NULL CHECK (stock >= 0),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- payments
CREATE TABLE payments (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id      uuid NOT NULL,
    amount_cents  integer NOT NULL CHECK (amount_cents >= 0),
    status        text NOT NULL CHECK (status IN ('captured', 'declined')),
    card_last4    text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON payments (order_id);

-- orders
CREATE TABLE orders (
    id              uuid PRIMARY KEY,
    customer_email  text NOT NULL,
    status          text NOT NULL CHECK (status IN ('paid', 'fulfilled', 'failed')),
    total_cents     integer NOT NULL,
    payment_id      uuid,
    created_at      timestamptz NOT NULL DEFAULT now(),
    fulfilled_at    timestamptz
);
CREATE TABLE order_items (
    order_id          uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    sku               text NOT NULL,
    qty               integer NOT NULL CHECK (qty > 0),
    unit_price_cents  integer NOT NULL,
    PRIMARY KEY (order_id, sku)
);

INSERT INTO products (sku, name, price_cents, pack_size, stock) VALUES
  ('KB-101', 'Mechanical keyboard',       8900, 1, 1000000),
  ('MS-202', 'Wireless mouse',            2900, 1, 1000000),
  ('HD-303', 'Noise-cancelling headset', 15900, 1, 1000000),
  ('MN-404', '27" monitor',              24900, 1, 1000000),
  ('CB-505', 'USB-C cable (3-pack)',      1500, 3, 1000000),
  ('ST-606', 'Laptop stand',              3900, 1, 1000000),
  ('WC-707', '1080p webcam',              5900, 1, 1000000),
  ('DS-808', 'Desk mat',                  1900, 1, 1000000),
  ('LP-909', 'LED desk lamp',             3400, 1, 1000000),
  ('HB-110', 'USB-C hub',                 4900, 1, 1000000),
  ('SS-211', 'Portable SSD 1TB',          9900, 1, 1000000),
  ('BT-312', 'AA batteries (8-pack)',      900, 8, 1000000);
