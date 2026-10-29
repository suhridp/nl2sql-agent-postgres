-- Deterministic synthetic data with skew (a few customers and products account for much of the
-- activity), so join order and index choices matter. Run after schema.sql with schema=public.
SET search_path = public;
SELECT setseed(0.42);

INSERT INTO customers
SELECT g,
       'Customer ' || g,
       'customer' || g || '@example.test',
       (ARRAY['consumer','consumer','consumer','corporate','small_business'])[1 + floor(random()*5)::int],
       (ARRAY['IN','IN','US','US','US','GB','DE','BR','FR','JP'])[1 + floor(random()*10)::int],
       CASE WHEN random() < 0.12 THEN NULL
            ELSE (ARRAY['Springfield','Riverton','Lakeside','Hillview','Fairmont','Oakdale','Maplewood','Brookfield'])[1 + floor(random()*8)::int] END,
       DATE '2021-01-01' + floor(random()*1460)::int
FROM generate_series(1, 20000) g;

INSERT INTO products
SELECT g,
       'Product ' || g,
       (ARRAY['electronics','books','toys','home','sports','beauty','grocery','fashion'])[1 + floor(random()*8)::int],
       CASE WHEN random() < 0.15 THEN NULL
            ELSE (ARRAY['Acme','Globex','Initech','Umbrella','Stark','Wayne','Hooli','Vandelay'])[1 + floor(random()*8)::int] END,
       round((4 + random()^2 * 396)::numeric, 2),
       DATE '2020-01-01' + floor(random()*2000)::int
FROM generate_series(1, 800) g;

INSERT INTO orders
SELECT g,
       1 + floor(20000 * random()^2.2)::int,
       DATE '2024-01-01' + floor(random()*731)::int,
       CASE WHEN r < 0.78 THEN 'completed' WHEN r < 0.87 THEN 'cancelled' WHEN r < 0.95 THEN 'returned' ELSE 'pending' END,
       (ARRAY['web','web','mobile','mobile','store'])[1 + floor(random()*5)::int]
FROM (SELECT g, random() AS r FROM generate_series(1, 150000) g) s;

INSERT INTO order_items
SELECT o.order_id, l.line_no, p.pid, 1 + floor(random()^3 * 6)::int,
       round((pr.list_price * (0.85 + random()*0.15))::numeric, 2),
       CASE WHEN random() < 0.7 THEN 0 ELSE round((0.05 + random()*0.25)::numeric, 3) END
FROM (SELECT order_id, 1 + floor(random()^1.5 * 5)::int AS n_lines FROM orders) o
CROSS JOIN LATERAL generate_series(1, o.n_lines) l(line_no)
CROSS JOIN LATERAL (SELECT 1 + floor(800 * random()^1.8)::int + (o.order_id * 0) + (l.line_no * 0) AS pid) p
JOIN products pr ON pr.product_id = p.pid;

INSERT INTO payments
SELECT row_number() OVER (ORDER BY o.order_id, part), o.order_id,
       (ARRAY['card','card','card','paypal','gift_card','bank_transfer'])[1 + floor(random()*6)::int],
       round(t.total / o.parts, 2),
       o.order_date + make_interval(hours => floor(random()*72)::int)
FROM (SELECT order_id, order_date, CASE WHEN random() < 0.08 THEN 2 ELSE 1 END AS parts
      FROM orders WHERE status IN ('completed','returned')) o
JOIN (SELECT order_id, sum(quantity * unit_price * (1 - discount)) AS total FROM order_items GROUP BY order_id) t
  USING (order_id)
CROSS JOIN LATERAL generate_series(1, o.parts) part;

INSERT INTO returns
SELECT row_number() OVER (ORDER BY oi.order_id, oi.line_no), oi.order_id, oi.product_id,
       (ARRAY['damaged','wrong_item','not_as_described','changed_mind','changed_mind',NULL])[1 + floor(random()*6)::int],
       o.order_date + 3 + floor(random()*27)::int
FROM order_items oi JOIN orders o USING (order_id)
WHERE o.status = 'returned' AND (oi.line_no = 1 OR random() < 0.3);

INSERT INTO reviews
SELECT row_number() OVER (ORDER BY o.order_id, oi.line_no), oi.product_id, o.customer_id,
       (ARRAY[1,2,3,4,4,5,5,5])[1 + floor(random()*8)::int],
       o.order_date + 5 + floor(random()*40)::int
FROM orders o JOIN order_items oi USING (order_id)
WHERE o.status = 'completed' AND random() < 0.15;

ANALYZE customers, products, orders, order_items, payments, returns, reviews;
