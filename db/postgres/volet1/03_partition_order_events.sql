-- Volet 1 : partitionnement par date.
--
-- Pourquoi pas orders.orders ? PostgreSQL exige que toute clé primaire ou contrainte UNIQUE d'une
-- table partitionnée contienne la colonne de partition (created_at). Or le service Commandes repose
-- sur UNIQUE(idempotency_key) (ON CONFLICT) et sur la clé étrangère order_items -> orders(id).
-- On partitionne donc une table d'historique, qui ne grossit que par ajout : orders.order_events,
-- alimentée automatiquement par un trigger (aucun changement dans le code des services).
\set ON_ERROR_STOP on

CREATE TABLE IF NOT EXISTS orders.order_events (
    id           bigint GENERATED ALWAYS AS IDENTITY,
    order_id     integer     NOT NULL,       -- réf. orders.orders (même schéma, mais pas de FK : table de journal)
    event_type   text        NOT NULL CHECK (event_type IN ('created', 'status_changed')),
    status       text        NOT NULL,
    total_cents  integer     NOT NULL CHECK (total_cents >= 0),
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, occurred_at)            -- la clé de partition doit faire partie de la clé primaire
) PARTITION BY RANGE (occurred_at);

-- Un index créé sur le parent est créé automatiquement sur chaque partition
CREATE INDEX IF NOT EXISTS idx_order_events_order_id ON orders.order_events (order_id);

-- Partition de secours : un événement hors des plages ne fait jamais échouer une commande
CREATE TABLE IF NOT EXISTS orders.order_events_default PARTITION OF orders.order_events DEFAULT;

-- Crée la partition du mois contenant p_month (sans effet si elle existe). À appeler chaque mois.
CREATE OR REPLACE FUNCTION orders.create_order_events_partition(p_month date) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
    v_start date := date_trunc('month', p_month)::date;
    v_name  text := 'order_events_' || to_char(v_start, 'YYYY_MM');
BEGIN
    IF to_regclass('orders.' || v_name) IS NULL THEN
        EXECUTE format('CREATE TABLE orders.%I PARTITION OF orders.order_events FOR VALUES FROM (%L) TO (%L)',
                       v_name, v_start, (v_start + interval '1 month')::date);
    END IF;
    RETURN v_name;
END $$;

-- 12 mois d'historique + 3 mois à venir
SELECT orders.create_order_events_partition((date_trunc('month', now()) + make_interval(months => m))::date)
FROM generate_series(-12, 3) m;

-- Trigger : chaque création de commande et chaque changement de statut laisse une trace
CREATE OR REPLACE FUNCTION orders.log_order_event() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO orders.order_events (order_id, event_type, status, total_cents, occurred_at)
        VALUES (NEW.id, 'created', NEW.status, NEW.total_cents, NEW.created_at);
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO orders.order_events (order_id, event_type, status, total_cents)
        VALUES (NEW.id, 'status_changed', NEW.status, NEW.total_cents);
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS trg_order_events ON orders.orders;
CREATE TRIGGER trg_order_events
    AFTER INSERT OR UPDATE OF status ON orders.orders
    FOR EACH ROW EXECUTE FUNCTION orders.log_order_event();

-- Droits : le service Commandes écrit dans le journal (les droits sur le parent suffisent)
GRANT SELECT, INSERT ON orders.order_events TO orders_svc;

-- Reprise de l'historique des commandes déjà présentes (une seule fois)
INSERT INTO orders.order_events (order_id, event_type, status, total_cents, occurred_at)
SELECT o.id, 'created', o.status, o.total_cents, o.created_at
FROM orders.orders o
WHERE NOT EXISTS (SELECT 1 FROM orders.order_events e WHERE e.order_id = o.id);

ANALYZE orders.order_events;
