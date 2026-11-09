-- Données de démo minimales (entrepôts + clients). Le catalogue se remplit via GraphQL.
INSERT INTO inventory.warehouses (name, capacity_units) VALUES
    ('Paris-Nord', 10000), ('Lyon-Est', 5000);
INSERT INTO orders.customers (name, email) VALUES
    ('Léa Martin', 'lea.martin@example.com'), ('Karim Benali', 'karim.benali@example.com');
