-- A small twin database in schema "edge", hand-built to break rewrites and answers that are only
-- right by coincidence on the main data: NULLs in every nullable column, customers with no orders,
-- products never sold, an order with no lines, exact ties, year-boundary dates, split payments,
-- duplicate names. Equivalence of query rewrites is checked on BOTH public and edge.
SET search_path = edge;

INSERT INTO customers VALUES
 (1,'Ana','ana@example.test','consumer','IN','Springfield','2021-03-01'),
 (2,'Ben','ben@example.test','corporate','US',NULL,'2022-07-15'),
 (3,'Ana','ana2@example.test','small_business','US','Lakeside','2023-12-31'),
 (4,'Chen','chen@example.test','consumer','GB',NULL,'2024-01-01'),
 (5,'Dara','dara@example.test','consumer','DE','Oakdale','2025-06-30'),
 (6,'Eli','eli@example.test','corporate','BR','Riverton','2025-12-31');   -- never orders
INSERT INTO products VALUES
 (1,'Lamp','home','Acme',20.00,'2020-01-01'),
 (2,'Novel','books',NULL,10.00,'2021-05-05'),
 (3,'Drone','electronics','Stark',300.00,'2023-02-02'),
 (4,'Ball','sports','Acme',10.00,'2024-12-31'),
 (5,'Kettle','home',NULL,25.00,'2025-01-01');                             -- never sold
INSERT INTO orders VALUES
 (1,1,'2024-01-01','completed','web'),
 (2,1,'2024-12-31','completed','mobile'),
 (3,2,'2025-01-01','completed','store'),
 (4,3,'2025-01-01','returned','web'),
 (5,4,'2025-06-30','cancelled','web'),
 (6,4,'2025-07-01','completed','mobile'),
 (7,5,'2025-12-31','pending','web'),
 (8,2,'2025-03-15','completed','web');                                   -- no lines
INSERT INTO order_items VALUES
 (1,1,1,1,20.00,0),(1,2,2,2,10.00,0),
 (2,1,4,2,10.00,0),
 (3,1,3,1,300.00,0.3),(3,2,1,1,20.00,0),
 (4,1,3,1,300.00,0),(4,2,2,1,10.00,0),
 (5,1,4,1,10.00,0),
 (6,1,1,1,20.00,0),(6,2,4,2,10.00,0.1),
 (7,1,2,3,10.00,0);
INSERT INTO payments VALUES
 (1,1,'card',40.00,'2024-01-01 10:00'),
 (2,2,'paypal',20.00,'2024-12-31 23:59'),
 (3,3,'card',115.00,'2025-01-02 09:00'),(4,3,'gift_card',115.00,'2025-01-02 09:05'),
 (5,4,'bank_transfer',310.00,'2025-01-03 12:00'),
 (6,6,'card',38.00,'2025-07-01 08:00');
INSERT INTO returns VALUES
 (1,4,3,'damaged','2025-01-10'),
 (2,4,2,NULL,'2025-01-10');
INSERT INTO reviews VALUES
 (1,1,1,5,'2024-01-10'),(2,1,4,1,'2025-07-10'),
 (3,3,2,4,'2025-01-20'),(4,4,1,5,'2025-01-05'),
 (5,2,1,3,'2024-02-01');
ANALYZE customers, products, orders, order_items, payments, returns, reviews;
