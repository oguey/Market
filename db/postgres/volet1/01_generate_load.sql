-- Volet 1 : jeu de données volumineux pour montrer les index (EXPLAIN) et le partitionnement.
-- Les lignes de test sont repérables (idempotency_key 'load-test-%', emails '%@example.test')
-- et se suppriment avec 00_cleanup_load.sql. Rejouer ce script ne recrée pas les mêmes lignes.
\set ON_ERROR_STOP on
\timing on

INSERT INTO orders.customers (name, email)
SELECT 'Client ' || g, 'client' || g || '@example.test'
FROM generate_series(1, 20000) g
ON CONFLICT (email) DO NOTHING;

-- 300 000 commandes : ~12 mois d'historique, dont une partie depuis minuit (pour « les commandes du jour »)
INSERT INTO orders.orders (customer_id, status, total_cents, created_at, idempotency_key)
SELECT ids[1 + floor(random() * array_length(ids, 1))::int],
       (ARRAY['paid','paid','paid','paid','paid','paid','paid','preparing','delivered','delivered',
              'delivered','payment_failed','cancelled'])[1 + floor(random() * 13)::int],
       500 + floor(random() * 30000)::int,
       CASE WHEN random() < 0.005
            THEN date_trunc('day', now()) + random() * (now() - date_trunc('day', now()))
            ELSE now() - random() * interval '365 days' END,
       'load-test-' || g
FROM generate_series(1, 300000) g
CROSS JOIN (SELECT array_agg(id) AS ids FROM orders.customers WHERE email LIKE '%@example.test') c
ON CONFLICT (idempotency_key) DO NOTHING;

-- 2 lignes par commande, sur 50 produits
INSERT INTO orders.order_items (order_id, product_id, quantity, unit_price_cents)
SELECT o.id,
       1 + floor(random() * 50)::int,
       1 + floor(random() * 5)::int,
       500 + floor(random() * 30000)::int
FROM orders.orders o
CROSS JOIN generate_series(1, 2)
WHERE o.idempotency_key LIKE 'load-test-%'
  AND NOT EXISTS (SELECT 1 FROM orders.order_items i WHERE i.order_id = o.id);

ANALYZE orders.customers;
ANALYZE orders.orders;
ANALYZE orders.order_items;

SELECT (SELECT count(*) FROM orders.customers)   AS clients,
       (SELECT count(*) FROM orders.orders)      AS commandes,
       (SELECT count(*) FROM orders.order_items) AS lignes;
