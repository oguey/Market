# Market — microservices

Four data-oriented services for the Market e-commerce platform. Design decisions are in **[docs/ADR.md](docs/ADR.md)**.

| Service | Port | Data | API | Calls |
|---------|------|------|-----|-------|
| **orders** | 8001 | PostgreSQL `orders` | REST `/v1/orders`, `/v1/customers` | catalog, inventory, payments |
| **inventory** | 8002 | PostgreSQL `inventory` | REST `/v1/inventory`, `/v1/warehouses`, `/v1/reservations` | – |
| **payments** | 8003 | PostgreSQL `payments` | REST `/v1/payments` (refuses > 200 €) | – |
| **catalog** | 8004 | MongoDB `catalog` | GraphQL `/v1/graphql` | – |

Every service exposes `/health`, `/metrics` (Prometheus) and `/docs` (Swagger).
API contracts: `services/<svc>/openapi.json` and `services/catalog/schema.graphql`.

## Run

```bash
cp .env.exemple .env          # then change the passwords and JWT_SECRET
docker compose up --build
```

> The DB init scripts only run on an **empty** volume. If you started the databases before this change,
> run `docker compose down -v` once.

Open the frontend at **http://localhost:3000**. It provides the overview, orders, catalog, logistics
and service-health pages. The UI reads the existing REST and GraphQL APIs and refreshes data every
30 seconds. Kafka, Grafana and a dedicated login service are not part of the current compose setup;
the interface reports them as unavailable rather than displaying sample data.

To sign in to the frontend, generate a local demo token and paste it into the login dialog:

```bash
docker compose exec orders python -m common.devtoken analyst
```

The JWT is kept in the current browser tab's session storage. Use `admin` instead of `analyst` for
the admin role. The backend remains responsible for validating the token and authorizing each API call.

For direct API requests, get a token as follows:

```bash
TOKEN=$(docker compose exec -T orders python -m common.devtoken admin)
H="Authorization: Bearer $TOKEN"

curl -s localhost:8004/v1/graphql -H "$H" -H 'Content-Type: application/json' -d '{"query":
  "mutation { createProduct(input: {productId: 1, name: \"Casque\", category: \"audio\", priceCents: 7990, attributes: {battery_hours: 30}}) { productId } }"}'
curl -s -X PUT localhost:8002/v1/inventory/1 -H "$H" -H 'Content-Type: application/json' -d '{"warehouse_id": 1, "quantity": 10}'
curl -s localhost:8001/v1/orders -H "$H" -H 'Idempotency-Key: demo-1' -H 'Content-Type: application/json' \
     -d '{"customer_id": 1, "items": [{"product_id": 1, "quantity": 2}]}'      # -> "status": "paid"
```

Demos for the jury:
- **Saga:** order 3 × 79.90 € (> 200 €) → `payment_failed`, and the stock is back (`GET /v1/inventory/1`).
- **Circuit breaker:** `docker compose stop inventory`, then post orders → 503, and from the second order on `circuit open` (no order even written). `circuit_breaker_state{dependency="inventory"} 2` shows in `localhost:8001/metrics`.
- **Idempotency:** post the same order twice with the same `Idempotency-Key` → same order id, stock reserved once.
- **RBAC:** a token from `devtoken analyst` on `POST /v1/orders` → 403. No token → 401.

## Rights matrix

| Action | admin | catalog_manager | analyst | logistics | service_account |
|---|:-:|:-:|:-:|:-:|:-:|
| `GET /v1/orders` | L | – | L | L | L |
| `POST /v1/orders`, `POST /v1/customers` | E | – | – | – | E |
| `PATCH /v1/orders/{id}/status` | E | – | – | – | – |
| GraphQL catalog queries | L | L | L | L | L |
| GraphQL create / update / archive | E | E | – | – | – |
| GraphQL `deleteProduct` | E | – | – | – | – |
| `GET /v1/warehouses` | L | – | L | L | – |
| `GET /v1/inventory/{id}` | L | L | L | L | L |
| `PUT /v1/inventory/{id}` | E | – | – | E | E |
| `/v1/reservations*`, `/v1/payments*` | E | – | – | – | E |

## Tests

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The tests start a **real PostgreSQL 16 and MongoDB 7 in-process** (no Docker) and apply the real `db/` init scripts.
CI runs them on every push (`.github/workflows/ci.yml`). If you change an API, run `python scripts/export_contracts.py`.

## Layout

```
services/
  common/      auth (JWT + require_roles), resilience (retry/breaker/bulkhead), observability, db
  orders/      main.py (API) + saga.py (order orchestration)
  inventory/   payments/   catalog/ (schema.graphql + main.py)
  Dockerfile   one recipe, --build-arg SERVICE=<name>
db/            init scripts (schemas, tables, roles, seed)
tests/         unit + API + end-to-end saga + isolation + contract tests
docs/ADR.md    architecture decisions
```


should configure it(them) to spark.eventLog.gcMetrics.youngGenerationGarbageCollectors or spark.eventLog.gcMetrics.oldGenerationGarbageCollectors
  order_items: 3 lues, 3 apres dedoublonnage, 3 gardees, 0 rejetees
  warehouses: 2 lues, 2 apres dedoublonnage, 2 gardees, 0 rejetees
  stock: 3 lues, 3 apres dedoublonnage, 3 gardees, 0 rejetees
  reservations: 2 lues, 2 apres dedoublonnage, 2 gardees, 0 rejetees
  reservation_items: 2 lues, 2 apres dedoublonnage, 2 gardees, 0 rejetees
  payments: 2 lues, 2 apres dedoublonnage, 2 gardees, 0 rejetees
  products: 3 lues, 3 apres dedoublonnage, 3 gardees, 0 rejetees
26/11/16 20:25:42 WARN AbstractS3ACommitterFactory: Using standard FileOutputCommitter to commit work. This is slow and potentially unsafe.
26/11/16 20:25:42 WARN AbstractS3ACommitterFactory: Using standard FileOutputCommitter to commit work. This is slow and potentially unsafe.
Rapport :                                                                       
+-----------------+---------+-----------------+---------+-------------+
|table            |rows_read|rows_deduplicated|rows_kept|rows_rejected|
+-----------------+---------+-----------------+---------+-------------+
|customers        |2        |2                |2        |0            |
|orders           |3        |3                |3        |0            |
|order_items      |3        |3                |3        |0            |
|warehouses       |2        |2                |2        |0            |
|stock            |3        |3                |3        |0            |
|reservations     |2        |2                |2        |0            |
|reservation_items|2        |2                |2        |0            |
|payments         |2        |2                |2        |0            |
|products         |3        |3                |3        |0            |
+-----------------+---------+-----------------+---------+-------------+

PS C:\Users\lenovo\Desktop\Market> 
metrics2.properties
Tables lues dans staging : customers, orders, order_items, products, stock, warehouses, payments, reservations, reservation_items
26/11/16 20:37:49 WARN GarbageCollectionMetrics: To enable non-built-in garbage collector(s) List(G1 Concurrent GC), usersshould configure it(them) to spark.eventLog.gcMetrics.youngGenerationGarbageCollectors or spark.eventLog.gcMetrics.oldGenerationGarbageCollectors
Controles qualite :
+-------------------------------+--------+----------+------+                    
|check_name                     |severity|violations|result|
+-------------------------------+--------+----------+------+
|customers: id unique           |error   |0         |OK    |
|orders: id unique              |error   |0         |OK    |
|order_items: id unique         |error   |0         |OK    |
|products: product_id unique    |error   |0         |OK    |
|customers: email unique        |error   |0         |OK    |
|orders -> customers            |error   |0         |OK    |
|order_items -> orders          |error   |0         |OK    |
|payments -> orders             |error   |0         |OK    |
|stock -> warehouses            |error   |0         |OK    |
|order_items -> products        |warn    |0         |OK    |
|orders.total = somme des lignes|warn    |0         |OK    |
+-------------------------------+--------+----------+------+

  curated/customers: 2 lignes (delta)                                           
  curated/orders: 3 lignes (delta)
  curated/order_items: 3 lignes (delta)
  curated/products: 3 lignes (delta)
  curated/stock: 3 lignes (delta)
  curated/warehouses: 2 lignes (delta)
  curated/payments: 2 lignes (delta)
  curated/reservations: 2 lignes (delta)
  curated/reservation_items: 2 lignes (delta)
26/11/16 20:38:09 WARN AbstractS3ACommitterFactory: Using standard FileOutputCommitter to commit work. This is slow and potentially unsafe.
26/11/16 20:38:09 WARN AbstractS3ACommitterFactory: Using standard FileOutputCommitter to commit work. This is slow and potentially unsafe.
Termine.                                                                        
PS C:\Users\lenovo\Desktop\Market> 

ationGarbageCollectors
26/11/16 20:45:17 WARN SparkStringUtils: Truncated the string representation of a plan since it was too large. This behavior can be adjusted by setting 'spark.sql.debug.maxToStringFields'.
                                                                                
--- consumption/sales_daily
+----------+------------+----------------+-------------+-------------+----------------+
|order_date|orders_total|orders_completed|orders_failed|revenue_euros|avg_basket_euros|
+----------+------------+----------------+-------------+-------------+----------------+
|2026-11-16|3           |1               |2            |159.8        |159.8           |
+----------+------------+----------------+-------------+-------------+----------------+

                                                                                
--- consumption/sales_by_category
+--------+----------+------------+-------------+                                
|category|units_sold|orders_count|revenue_euros|
+--------+----------+------------+-------------+
|audio   |2         |1           |159.8        |
+--------+----------+------------+-------------+

                                                                                
--- consumption/stock_by_warehouse
+------------+----------+--------------+--------------+--------------+-------------+
|warehouse_id|warehouse |capacity_units|units_in_stock|products_count|fill_rate_pct|
+------------+----------+--------------+--------------+--------------+-------------+
|1           |Paris-Nord|10000         |66            |3             |0.7          |
|2           |Lyon-Est  |5000          |0             |0             |0.0          |
+------------+----------+--------------+--------------+--------------+-------------+

