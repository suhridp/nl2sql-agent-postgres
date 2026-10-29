-- Retail analytics schema. Every table and column is documented with COMMENT ON, because the
-- "schema_prompt" systems pass these comments to the model and "zero_shot" does not.
-- Run as the owner role. Usage: psql -v schema=public -f schema.sql   (or schema=edge)
CREATE SCHEMA IF NOT EXISTS :"schema";
SET search_path = :"schema";
DROP TABLE IF EXISTS reviews, returns, payments, order_items, orders, products, customers CASCADE;

CREATE TABLE customers (
  customer_id  integer PRIMARY KEY,
  name         text    NOT NULL,
  email        text    NOT NULL UNIQUE,
  segment      text    NOT NULL CHECK (segment IN ('consumer','corporate','small_business')),
  country      text    NOT NULL,
  city         text,
  signup_date  date    NOT NULL
);
CREATE TABLE products (
  product_id   integer PRIMARY KEY,
  name         text    NOT NULL,
  category     text    NOT NULL,
  brand        text,
  list_price   numeric(10,2) NOT NULL,
  launched_on  date    NOT NULL
);
CREATE TABLE orders (
  order_id     integer PRIMARY KEY,
  customer_id  integer NOT NULL REFERENCES customers,
  order_date   date    NOT NULL,
  status       text    NOT NULL CHECK (status IN ('pending','completed','cancelled','returned')),
  channel      text    NOT NULL CHECK (channel IN ('web','mobile','store'))
);
CREATE TABLE order_items (
  order_id     integer NOT NULL REFERENCES orders,
  line_no      integer NOT NULL,
  product_id   integer NOT NULL REFERENCES products,
  quantity     integer NOT NULL CHECK (quantity > 0),
  unit_price   numeric(10,2) NOT NULL,
  discount     numeric(4,3) NOT NULL DEFAULT 0,
  PRIMARY KEY (order_id, line_no)
);
CREATE TABLE payments (
  payment_id   integer PRIMARY KEY,
  order_id     integer NOT NULL REFERENCES orders,
  method       text    NOT NULL CHECK (method IN ('card','paypal','gift_card','bank_transfer')),
  amount       numeric(12,2) NOT NULL,
  paid_at      timestamp NOT NULL
);
CREATE TABLE returns (
  return_id    integer PRIMARY KEY,
  order_id     integer NOT NULL REFERENCES orders,
  product_id   integer NOT NULL REFERENCES products,
  reason       text,
  returned_on  date    NOT NULL
);
CREATE TABLE reviews (
  review_id    integer PRIMARY KEY,
  product_id   integer NOT NULL REFERENCES products,
  customer_id  integer NOT NULL REFERENCES customers,
  rating       smallint NOT NULL CHECK (rating BETWEEN 1 AND 5),
  created_at   date    NOT NULL
);

CREATE INDEX ON orders (customer_id);
CREATE INDEX ON orders (order_date);
CREATE INDEX ON order_items (product_id);
CREATE INDEX ON payments (order_id);
CREATE INDEX ON reviews (product_id);
-- deliberately no index on returns.order_id, reviews.customer_id or customers.country

COMMENT ON TABLE customers   IS 'One row per registered customer.';
COMMENT ON COLUMN customers.segment     IS 'consumer, corporate or small_business';
COMMENT ON COLUMN customers.country     IS 'ISO 3166 alpha-2 code, e.g. IN, US, GB, DE, BR';
COMMENT ON COLUMN customers.city        IS 'May be NULL when the customer did not give one';
COMMENT ON TABLE products    IS 'Product catalogue.';
COMMENT ON COLUMN products.list_price   IS 'Current catalogue price in USD; the price actually paid is order_items.unit_price';
COMMENT ON COLUMN products.brand        IS 'May be NULL for unbranded products';
COMMENT ON TABLE orders      IS 'One row per order. Only status = completed counts as a sale.';
COMMENT ON COLUMN orders.status         IS 'pending, completed, cancelled or returned';
COMMENT ON COLUMN orders.channel        IS 'web, mobile or store';
COMMENT ON TABLE order_items IS 'Order lines. Line revenue = quantity * unit_price * (1 - discount).';
COMMENT ON COLUMN order_items.discount  IS 'Fraction between 0 and 0.3, e.g. 0.1 = 10% off';
COMMENT ON TABLE payments    IS 'Payments received; an order can have more than one payment.';
COMMENT ON TABLE returns     IS 'Returned products; one row per returned product line.';
COMMENT ON COLUMN returns.reason        IS 'damaged, wrong_item, not_as_described, changed_mind, or NULL if not given';
COMMENT ON TABLE reviews     IS 'Product ratings from 1 (worst) to 5 (best).';
