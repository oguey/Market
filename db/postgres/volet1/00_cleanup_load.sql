-- Supprime uniquement les données générées par 01_generate_load.sql
-- (les lignes d'order_items partent avec leur commande : ON DELETE CASCADE).
DELETE FROM orders.orders    WHERE idempotency_key LIKE 'load-test-%';
DELETE FROM orders.customers WHERE email LIKE '%@example.test';
DO $$ BEGIN
  IF to_regclass('orders.order_events') IS NOT NULL THEN EXECUTE 'TRUNCATE orders.order_events'; END IF;
END $$;
VACUUM ANALYZE orders.orders;
