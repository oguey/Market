-- Volet 1 : preuve des index avec EXPLAIN (avant / après).
-- « AVANT » = on retire l'index dans une transaction, puis ROLLBACK : l'index réel n'est jamais perdu.
-- À lancer après 01_generate_load.sql. Repère dans chaque plan : le type de scan et « Execution Time ».
\set ON_ERROR_STOP on
\pset pager off
ANALYZE orders.orders;
ANALYZE orders.order_items;

\echo
\echo '#################### REQUÊTE 1 : les commandes du jour par statut (onglet Vue d''ensemble)'
\echo '==== AVANT : sans index sur created_at (attendu : Seq Scan = lecture de toute la table)'
BEGIN;
DROP INDEX orders.idx_orders_created_at;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT status, count(*) AS commandes, sum(total_cents) AS ca_cents
FROM orders.orders
WHERE created_at >= date_trunc('day', now())
GROUP BY status;
ROLLBACK;

\echo '==== APRÈS : avec idx_orders_created_at (attendu : Index / Bitmap Scan, quelques pages lues)'
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT status, count(*) AS commandes, sum(total_cents) AS ca_cents
FROM orders.orders
WHERE created_at >= date_trunc('day', now())
GROUP BY status;

\echo
\echo '#################### REQUÊTE 2 : les 10 dernières commandes d''un client (onglet Commandes)'
\echo '==== AVANT : sans index sur customer_id'
BEGIN;
DROP INDEX orders.idx_orders_customer_id;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT id, status, total_cents, created_at
FROM orders.orders
WHERE customer_id = 1234
ORDER BY created_at DESC
LIMIT 10;
ROLLBACK;

\echo '==== APRÈS : avec idx_orders_customer_id'
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT id, status, total_cents, created_at
FROM orders.orders
WHERE customer_id = 1234
ORDER BY created_at DESC
LIMIT 10;

\echo
\echo '#################### REQUÊTE 3 : quantités vendues pour un produit (ventes par produit)'
\echo '==== AVANT : sans index sur product_id'
BEGIN;
DROP INDEX orders.idx_order_items_product;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT sum(quantity) FROM orders.order_items WHERE product_id = 7;
ROLLBACK;

\echo '==== APRÈS : avec idx_order_items_product'
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT sum(quantity) FROM orders.order_items WHERE product_id = 7;
