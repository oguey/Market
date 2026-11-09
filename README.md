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

Get a token (until the auth service exists):

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
