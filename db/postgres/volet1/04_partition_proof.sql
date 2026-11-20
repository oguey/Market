-- Volet 1 : preuve du partitionnement (à lancer après 03_partition_order_events.sql).
\pset pager off

\echo '==== 1) Les partitions et leur nombre de lignes'
SELECT c.relname AS partition, pg_get_expr(c.relpartbound, c.oid) AS plage,
       (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM orders.%I', c.relname), false, true, '')))[1]::text AS lignes
FROM pg_inherits i
JOIN pg_class c ON c.oid = i.inhrelid
WHERE i.inhparent = 'orders.order_events'::regclass
ORDER BY c.relname;

\echo
\echo '==== 2) Élagage (partition pruning) : le mois courant seulement (attendu : UNE seule partition lue)'
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF)
SELECT status, count(*)
FROM orders.order_events
WHERE occurred_at >= date_trunc('month', now())
  AND occurred_at <  date_trunc('month', now()) + interval '1 month'
GROUP BY status;

\echo
\echo '==== 3) Sans filtre de date : toutes les partitions sont lues (comparaison)'
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF)
SELECT status, count(*) FROM orders.order_events GROUP BY status;

\echo
\echo '==== 4) Archivage : détacher un vieux mois est instantané (exemple, non exécuté)'
\echo '-- ALTER TABLE orders.order_events DETACH PARTITION orders.order_events_2025_11;'
