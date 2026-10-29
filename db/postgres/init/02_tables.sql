-- schéma orders
CREATE TABLE IF NOT EXISTS orders.customers (
    id      serial PRIMARY KEY,
    name    text NOT NULL,
    email   text NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS orders.orders (
    id            serial PRIMARY KEY,
    customer_id   integer NOT NULL REFERENCES orders.customers (id),
    status        text NOT NULL,
    total_cents   integer NOT NULL CHECK (total_cents >= 0),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders.order_items (
    id                serial PRIMARY KEY,
    order_id          integer NOT NULL REFERENCES orders.orders (id) ON DELETE CASCADE,
    product_id        integer NOT NULL,   -- réf. MongoDB catalog (lien applicatif)
    quantity          integer NOT NULL CHECK (quantity > 0),
    unit_price_cents  integer NOT NULL CHECK (unit_price_cents >= 0)
);

CREATE INDEX IF NOT EXISTS idx_orders_customer_id   ON orders.orders (customer_id);
CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON orders.order_items (order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product  ON orders.order_items (product_id);

-- schéma inventory
CREATE TABLE IF NOT EXISTS inventory.warehouses (
    id              serial PRIMARY KEY,
    name            text NOT NULL,
    capacity_units  integer NOT NULL CHECK (capacity_units >= 0)
);

CREATE TABLE IF NOT EXISTS inventory.stock (
    product_id    integer NOT NULL,       -- réf. MongoDB catalog (lien applicatif)
    warehouse_id  integer NOT NULL REFERENCES inventory.warehouses (id),
    quantity      integer NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    PRIMARY KEY (product_id, warehouse_id)
);

CREATE INDEX IF NOT EXISTS idx_stock_warehouse_id ON inventory.stock (warehouse_id);

-- schéma payments
CREATE TABLE IF NOT EXISTS payments.payments (
    id            serial PRIMARY KEY,
    order_id      integer NOT NULL,       -- réf. orders.orders (lien applicatif)
    amount_cents  integer NOT NULL CHECK (amount_cents >= 0),
    status        text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments.payments (order_id);

-- schéma auth
CREATE TABLE IF NOT EXISTS auth.users (
    id             serial PRIMARY KEY,
    email          text NOT NULL UNIQUE,
    password_hash  text NOT NULL,
    is_active      boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS auth.roles (
    id           serial PRIMARY KEY,
    name         text NOT NULL UNIQUE,
    description  text
);

CREATE TABLE IF NOT EXISTS auth.user_roles (
    user_id  integer NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    role_id  integer NOT NULL REFERENCES auth.roles (id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);

CREATE INDEX IF NOT EXISTS idx_user_roles_role_id ON auth.user_roles (role_id);