# Architecture Decision Records — Market microservices

Format from the project brief: **Status · Context · Options · Decision · Consequences**.
Statuses: *proposed*, *accepted*, *superseded*.

| # | Decision | Status |
|---|----------|--------|
| [001](#adr-001--four-services-one-database-per-service) | 4 services, one database per service | accepted |
| [002](#adr-002--postgresql-for-orders-inventory-and-payments) | PostgreSQL for orders, inventory, payments | accepted |
| [003](#adr-003--mongodb--schema-first-graphql-for-the-catalog) | MongoDB + schema-first GraphQL for the catalog | accepted |
| [004](#adr-004--orchestrated-saga--idempotency-for-orders) | Orchestrated saga + idempotency for orders | accepted |
| [005](#adr-005--resilience-timeout-retry-circuit-breaker-bulkhead) | Timeout, retry, circuit breaker, bulkhead | accepted |
| [006](#adr-006--jwt-checked-in-every-service-rbac-in-code) | JWT checked in every service, RBAC in code | accepted |
| [007](#adr-007--rest-contracts-versioned-snapshots-under-v1) | REST contracts: versioned snapshots under `/v1` | accepted |
| [008](#adr-008--tests-against-real-databases-without-docker) | Tests against real databases, without Docker | accepted |

---

## ADR-001 — Four services, one database per service

**Status:** accepted

**Context.** The brief asks for at least 3 data-oriented microservices, each owning its data.
Ordering touches the catalog (price), the stock (reservation) and the payment. The team is small,
so we need to keep the infrastructure light.

**Options.**
1. *Monolith on one schema.* Simplest, but joins and foreign keys across domains couple everything, and it fails the brief.
2. *One PostgreSQL server per service.* Strongest isolation, but 3 extra containers, backups and configs to look after.
3. *One PostgreSQL server, one schema + one SQL role per service* (catalog on MongoDB).

**Decision.** Option 3. We have **Orders** (`orders` schema), **Inventory** (`inventory`), **Payments**
(`payments`) and **Catalog** (MongoDB `catalog` collection). Each service logs in with its own role
(`orders_svc`, …), which has rights only on its own schema, so the database itself enforces
"a service never reads another service's tables". Only Orders calls other services. Inventory, Payments
and Catalog call nobody.

**Consequences.**
- ➕ Real isolation at a low ops cost. `tests/test_isolation.py` shows that cross-schema reads fail with `permission denied`.
- ➖ There are no foreign keys or joins across services. `product_id` and `order_id` are links that the code checks (Orders asks Catalog for every `product_id`).
- ➖ The PostgreSQL server is a shared failure point and a "noisy neighbour" risk. *Plan B:* move a schema onto its own server. This only needs a change to `POSTGRES_HOST`, with no code change.

---

## ADR-002 — PostgreSQL for orders, inventory and payments

**Status:** accepted

**Context.** An order and its lines must be saved completely or not at all. Stock must never go negative,
because overselling is the main business risk. Payments must never be duplicated. These are
correctness needs first: in CAP terms we choose **consistency (CP)** over availability.

**Options.**
1. *MongoDB.* Its schema is flexible, but multi-document transactions are heavier, and the data here is regular and relational.
2. *Cassandra.* It scales writes very well, but it is eventually consistent (BASE). A stock check followed by a decrement is unsafe.
3. *PostgreSQL.* ACID transactions, `CHECK (quantity >= 0)`, foreign keys inside a schema, and row locks (`SELECT … FOR UPDATE`).

**Decision.** PostgreSQL 16. Reservations lock the stock rows of each product in a fixed order (sorted
`product_id`), which avoids deadlocks between concurrent orders. They also take from several warehouses
when needed, all inside one transaction.

**Consequences.**
- ➕ Overselling is impossible: the `CHECK` constraint and the transaction protect it even if the code has a bug.
- ➖ It scales vertically first, then with read replicas. Partitioning `orders.orders` by `created_at` is the next step when volume requires it.

---

## ADR-003 — MongoDB + schema-first GraphQL for the catalog

**Status:** accepted

**Context.** Products do not share the same characteristics (battery life for a headset, size for shoes).
Each screen needs different fields. Catalog writes are rare and reads are frequent.

**Options.**
1. *PostgreSQL with a `jsonb` column.* One engine fewer, but the brief asks for a NoSQL engine, and indexing many varied attributes is less natural.
2. *MongoDB* with a `$jsonSchema` validator on the fixed fields and a free `attributes` object.
3. API style: *REST* (fixed payloads) vs *GraphQL* (the client picks its fields). GraphQL lib: *Strawberry* (code-first) vs *Ariadne* (schema-first).

**Decision.** MongoDB 7 behind a GraphQL API built with **Ariadne**. The contract
`services/catalog/schema.graphql` is written first, and resolvers implement it (contract-first). Indexes: a
unique index on `product_id`, plus indexes on `category` and `tags`, and a French **text index** on
`name + description` for search. Products that may have been ordered are **archived**
(`archiveProduct` → `status: inactive`) rather than deleted. Hard delete is admin-only.

**Consequences.**
- ➕ New attributes need no migration. The UI fetches exactly the fields it shows.
- ➖ There is no foreign key to PostgreSQL. Orders refuses unknown or archived `product_id`s (HTTP 422).
- ➖ Catalog cannot check whether a product was already ordered (it calls nobody, per ADR-001). This is why archive is the documented default.

---

## ADR-004 — Orchestrated saga + idempotency for orders

**Status:** accepted

**Context.** One order touches 3 databases. A distributed transaction is not possible across services.
If payment fails after stock was reserved, the stock must be given back. Clients and our own retries can
send the same request twice.

**Options.**
1. *Two-phase commit.* Blocking, not supported across HTTP services, and it couples availability.
2. *Choreographed saga* over Kafka events. Loosely coupled, but the flow is spread across services and harder to demo and debug.
3. *Orchestrated saga* driven by Orders over synchronous REST.

**Decision.** Option 3. Orders runs: catalog prices → save order `pending` (order + lines in one
transaction) → `POST /v1/reservations` → `POST /v1/payments` → `confirm`. Compensation:
`POST /v1/reservations/{id}/release`. Final statuses: `paid`, `payment_failed` (stock returned) and
`cancelled` (out of stock or dependency down). Idempotency at every hop:
- Orders: optional `Idempotency-Key` header (unique column). A replay returns the same order with HTTP 200.
- Inventory and Payments: keyed by `order_id` (PK / unique index). A replay never reserves or charges twice.

**Consequences.**
- ➕ The flow fits in one readable file (`services/orders/saga.py`), and retries are safe.
- ➖ The call is synchronous: Orders' latency is the sum of 3 calls.
- ➖ If Inventory is down *during* a compensation, the release is logged as `COMPENSATION PENDING`. *Plan B:* a small job that retries pending releases. They are idempotent, so retrying is safe. Not built yet.
- ➖ One payment attempt per order: a refused order is final, and a new order is needed.

---

## ADR-005 — Resilience: timeout, retry, circuit breaker, bulkhead

**Status:** accepted

**Context.** A slow or dead dependency must not freeze Orders or pile up threads. The jury wants to see
the circuit breaker open when the stock service is stopped.

**Options.**
1. *Libraries* (`tenacity` + `pybreaker`). Proven, but two more dependencies to configure together.
2. *Service mesh / gateway policies.* Powerful, but heavy for a prototype and invisible in the code.
3. *A small in-house module* (`services/common/resilience.py`, about 100 lines, unit-tested).

**Decision.** Option 3. Every outgoing call goes through **bulkhead** (a semaphore of 10 per dependency,
rejecting at once when full) → **circuit breaker** (opens after 3 failures, half-opens after 10 s, one
failed trial reopens it) → **retry** (2 retries with exponential backoff, on network errors and 5xx only,
never on 4xx) → **timeout** (2 s). Retrying is safe because every target endpoint is idempotent
(ADR-004). The breaker state is exported as the Prometheus gauge `circuit_breaker_state`. When a
circuit is open, Orders refuses new orders *before* writing anything (fail fast).

**Consequences.**
- ➕ Behaviour is explicit, testable with a fake clock, and visible in Grafana.
- ➖ Breaker state lives in each process. With several Orders replicas, each one learns on its own. That is acceptable at this scale.

---

## ADR-006 — JWT checked in every service, RBAC in code

**Status:** accepted

**Context.** Every API must require authentication (401) and enforce the brief's rights matrix (403).
The gateway checks tokens, but a service reachable from inside the network must not trust that blindly.

**Options.**
1. *Gateway only.* Simpler, but any internal caller bypasses all checks.
2. *Asymmetric JWT (RS256)*: only the auth service can sign. Better, but it needs key distribution and an auth service that does not exist yet.
3. *HS256 JWT with a shared `JWT_SECRET`*, verified again in each service, with a reusable `require_roles(...)` dependency.

**Decision.** Option 3 for now. Claims are `sub`, `roles` and `exp` only (no personal data). Each REST route
declares its roles with `Depends(require_roles("admin", "analyst"))`. GraphQL checks roles **per resolver**,
not only at the HTTP door, and returns `extensions.code = "FORBIDDEN"`. Service-to-service calls use a
short-lived (5 min) `service_account` token minted by Orders. Every 401/403 is logged with the user and the
action. When in doubt, access is denied.

**Consequences.**
- ➕ Defense in depth. The rights matrix is readable next to each route.
- ➖ Any holder of the secret can mint tokens. *Plan B (superseding ADR):* switch to RS256 once the auth service is built. Only `decode_token` changes.
- Until then, `python -m common.devtoken <role>` mints test tokens.

---

## ADR-007 — REST contracts: versioned snapshots under `/v1`

**Status:** accepted

**Context.** The brief asks for contract-first, versioned APIs, with OpenAPI declaring security.

**Options.**
1. *Hand-written OpenAPI YAML* + code generation. True contract-first, but two sources of truth with FastAPI.
2. *FastAPI-generated OpenAPI only.* No drift, but the contract is never reviewed as such.
3. *Generated OpenAPI, committed as a snapshot and checked by a test.*

**Decision.** Option 3 for REST (`services/<svc>/openapi.json`). `tests/test_contracts.py` fails when code
and contract drift apart, so every API change shows up in the diff as a contract change. Run
`python scripts/export_contracts.py` to update the snapshots. All routes are under `/v1`, and the specs
declare the `HTTPBearer` security scheme. GraphQL is truly schema-first (ADR-003). A breaking change goes
to `/v2` next to `/v1`.

**Consequences.** ➕ One source of truth plus explicit review. ➖ This is not strictly "written before the code". We accept that trade-off and document it here.

---

## ADR-008 — Tests against real databases, without Docker

**Status:** accepted

**Context.** The saga, row locks, `CHECK` constraints, SQL roles and the Mongo validator are the risky
parts. Mocks would not catch their bugs. Tests must run on every commit (CI) and on laptops where
Docker may be missing.

**Options.**
1. *Mocks / mongomock.* Fast, but no real SQL, no `$text`, no permission checks.
2. *Testcontainers.* Real databases, but they require Docker.
3. *In-process real servers*: `pgserver` (PostgreSQL 16 binaries) and `pymongo-inmemory` (MongoDB 7).

**Decision.** Option 3. The tests apply the **real** init scripts from `db/` (including the roles script
and the Mongo `$jsonSchema` validator). Each service connects with its own role. End-to-end tests wire
the real Orders app to the real Catalog, Inventory and Payments apps in-process. Failures are injected
with `httpx.MockTransport`.

**Consequences.**
- ➕ 54 tests in about 10 s, with no Docker. They cover the saga, idempotency, the circuit breaker, RBAC, isolation and contracts.
- ➖ The first run downloads MongoDB (about 100 MB). The in-process MongoDB build targets Ubuntu 22.04, and CI is pinned to it.
