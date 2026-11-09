-- Tables/colonnes ajoutées pour les microservices (saga + idempotence).
-- Exécuté avant 05_roles.sh, donc couvert par les GRANT de chaque service.

-- orders : une même requête (Idempotency-Key) ne crée qu'une seule commande
ALTER TABLE orders.orders ADD COLUMN IF NOT EXISTS idempotency_key text UNIQUE;
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders.orders (created_at DESC);

-- inventory : réservations de stock, une par commande (order_id = clé d'idempotence)
CREATE TABLE IF NOT EXISTS inventory.reservations (
    order_id    integer PRIMARY KEY,          -- réf. orders.orders (lien applicatif)
    status      text NOT NULL DEFAULT 'reserved'
                CHECK (status IN ('reserved', 'released', 'confirmed')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS inventory.reservation_items (
    order_id      integer NOT NULL REFERENCES inventory.reservations (order_id) ON DELETE CASCADE,
    product_id    integer NOT NULL,           -- réf. MongoDB catalog (lien applicatif)
    warehouse_id  integer NOT NULL REFERENCES inventory.warehouses (id),
    quantity      integer NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (order_id, product_id, warehouse_id)
);

-- payments : un seul paiement par commande (idempotence)
DROP INDEX IF EXISTS payments.idx_payments_order_id;
CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_order_id ON payments.payments (order_id);
