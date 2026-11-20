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
| [010](#adr-010--auth-service-bcrypt-login-access--refresh-jwt) | Auth service: bcrypt login, access + refresh JWT | accepted |
| [011](#adr-011--lakehouse-rustfs-four-zones-spark-and-delta) | Lakehouse: RustFS, four zones, Spark and Delta | accepted |
| [012](#adr-012--api-gateway-nginx-with-jwt-check-and-rate-limiting) | API gateway: NGINX with JWT check and rate limiting | accepted |
| [013](#adr-013--distributed-tracing-with-opentelemetry-and-jaeger) | Distributed tracing: OpenTelemetry + Jaeger | accepted |
| [014](#adr-014--real-time-kafka-events-spark-structured-streaming-payment-failure-alert) | Real time: Kafka, Spark Structured Streaming, payment-failure alert | accepted |
| [014](#adr-014--real-time-kafka-events-spark-structured-streaming-payment-failure-alert) | Real time: Kafka, Spark Structured Streaming, payment-failure alert | accepted |

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

---

## ADR-010 — Auth service: bcrypt login, access + refresh JWT

**Status:** accepted (supersedes the "Plan B" note of ADR-006 for token issuing; HS256 is kept)

**Context.** The brief requires a login service (bcrypt or argon2) with a refresh token. Until now tokens were
minted by `common.devtoken`, so no real user could sign in and place an order.

**Options.**
1. *Keep dev tokens only.* Nothing to build, but no real login and no demo for users.
2. *External identity provider (Keycloak).* Complete, but heavy for this project and one more thing to run.
3. *Small dedicated `auth` service* on PostgreSQL schema `auth`, passwords hashed with bcrypt, returning a short access JWT and a longer refresh JWT.

**Decision.** Option 3. `POST /v1/auth/login` checks email and password (bcrypt, cost 12, constant-time
even for unknown emails) and returns an access token (15 min) and a refresh token (7 days, `typ: refresh`).
`POST /v1/auth/refresh` issues a new access token and re-reads the roles from the database, so a disabled
user or a role change takes effect at the next refresh. Tokens keep the ADR-006 claims (`sub`, `roles`,
`iat`, `exp`): no email or personal data. `POST /v1/auth/users` (admin only) creates users. The `auth`
service uses a SQL role limited to its own schema. The front keeps tokens in `sessionStorage` and refreshes
silently on a 401.

**Consequences.**
- ➕ Real sign-in, the rights matrix of ADR-006 now applies to real users, passwords are never stored in clear.
- ➖ One more service to run. Tokens are still signed with a shared secret (HS256): move to RS256 if the services ever leave a single trust zone.
- ➖ A refresh token cannot be revoked before it expires (no server-side list). A revocation table is the next step if needed.

---

## ADR-011 — Lakehouse: RustFS, four zones, Spark and Delta

**Status:** accepted

**Context.** The brief asks for a data lake / lakehouse fed from the PostgreSQL and MongoDB services, with
zones, quality checks, a pseudonymised e-mail and a data catalogue. Everything must start with Docker and no cloud account.

**Options.**
1. *Cloud (ADLS Gen2 + Data Factory + Databricks).* Already practised in the retail360 lab, but it needs an account,
   costs money and the jury could not replay it.
2. *MinIO for object storage.* The obvious local choice, but MinIO stopped distributing its images in 2026
   (Docker Hub and quay.io removed them), so `docker compose up` fails.
3. *RustFS (S3-compatible), Spark and Delta Lake in Docker.*

**Decision.** Option 3.
- **Storage:** RustFS, S3 API, one bucket per zone: `raw` (faithful copy of the sources, Parquet, by extraction date),
  `staging` (deduplicated and normalised, rejected rows kept in `_rejected`), `curated` (trusted Delta tables),
  `consumption` (aggregates for BI). Raw and staging may hold personal data and are restricted.
- **Extraction:** a small Python job copies 8 PostgreSQL tables and the MongoDB `catalog` collection to `raw`. The
  `auth` schema is never extracted (password hashes).
- **Processing:** PySpark jobs, run on demand and chained by `depends_on`. The e-mail is replaced by a salted SHA-256
  (`email_hash`) and the name dropped in `curated`. Blocking quality checks (unique keys, valid references) stop the
  run before anything is written; softer checks only warn.
- **Delta Lake** for `curated` and `consumption`: atomic writes, history and schema enforcement, which makes the whole a lakehouse.
- **Catalogue:** generated from the real schemas into `docs/DATA_CATALOG.md`.

**Consequences.**
- ➕ Reproducible with one command, no cloud cost, personal data protected before analysis, every table traceable to a source.
- ➖ Batch only: the lake is a copy refreshed on demand, not in real time (the Kafka stream of the next volet covers freshness).
- ➖ Spark images are heavy (Java, about 1 GB) and the first build is slow.
- ➖ RustFS 1.0 is recent. The jobs only use the plain S3 API, so another S3 store (Garage, SeaweedFS) could replace it by changing the endpoint.

---

## ADR-012 — API gateway: NGINX with JWT check and rate limiting

**Status:** accepted

**Context.** The brief asks for an API gateway that verifies the JWT and limits the request rate. Until now the
front called every service directly, and nothing slowed down a brute-force on the login.

**Options.**
1. *Kong or Traefik.* Full-featured (plugins, dashboards), but one more heavy component with its own database or
   configuration model, for four small services.
2. *NGINX with a JWT module or scripting (njs, Lua).* Verifies the signature inside the gateway, but the secret must
   be duplicated there and the module is not in the standard image.
3. *The existing NGINX with `auth_request`.* For each protected call, NGINX asks the auth service (`GET /v1/auth/me`)
   whether the token is valid, then forwards the request or answers 401.

**Decision.** Option 3. The NGINX that already serves the front and routes `/api/...` becomes the single entry point.
- **JWT check:** `orders`, `inventory`, `payments`, `catalog` and the auth routes other than `login` and `refresh`
  require a valid access token. A missing, forged, expired or refresh token gets a JSON 401 without reaching the service.
- **Rate limiting** per client IP: 20 requests/s with a burst of 40 for the API, 10 per minute with a burst of 5 for
  `login`. Beyond that, a JSON 429 with `Retry-After`. The limit runs before the token check, so floods without a token are slowed too.
- The services still verify the token and the role themselves (ADR-006): the gateway is a first barrier, not the only one.

**Consequences.**
- ➕ One entry point, no new component, secret kept in the services only, login protected against brute force.
- ➖ One extra internal call per request (cheap: no database access in `/me`).
- ➖ Limits are per IP: clients behind the same address share a quota. Per-user limits would need a gateway that reads the claims.
- ➖ The service ports stay published for development, and `/health` and `/metrics` are public (the front's observability tab reads them). Close the ports and protect `/metrics` before any real deployment.

## ADR-013 — Distributed tracing with OpenTelemetry and Jaeger

**Status:** accepted

**Context.** Metrics (ADR on Prometheus/Grafana) say *that* something is slow or failing, not *where*. A single
order crosses Orders → Catalog → Inventory → Payments and writes to PostgreSQL. The brief asks for traces so
that one request can be followed across services.

**Options.**
1. *Correlation id in logs only.* Cheap, but no timing per hop and no visual of the call tree.
2. *Vendor agent (Datadog, New Relic).* Powerful, paid, and hides the mechanism.
3. *OpenTelemetry SDK + Jaeger.* Open standard, W3C `traceparent` header, free, UI included.

**Decision.** Option 3. `common/tracing.py` is called from `observability.setup()`, so all five services get it
without touching business code. It instruments FastAPI (server spans), httpx (client spans, which inject
`traceparent` into outgoing calls) and psycopg (one span per SQL query). `/health` and `/metrics` are excluded to
avoid noise. Spans go by OTLP/HTTP to Jaeger v2 (`jaeger:4318`, UI on port 16686). Tracing is **off unless
`OTEL_EXPORTER_OTLP_ENDPOINT` is set**, so tests and local runs without Jaeger are unaffected, and any setup error
only logs a warning: tracing must never take a service down.

**Consequences.**
- ➕ One order = one trace with the full call tree, durations per hop and per SQL query.
- ➕ Verified by `tests/test_tracing.py`: two real HTTP services share one trace id; health/metrics are not traced.
- ➖ Jaeger's default storage is in memory: traces are lost on restart. *Plan B:* Elasticsearch/OpenSearch or Tempo.
- ➖ Every request is sampled (fine for a prototype). *Plan B:* a ratio sampler in production.
- ➖ The NGINX gateway does not yet produce spans, so a trace starts at the first microservice.


## ADR-014 — Real time: Kafka events, Spark Structured Streaming, payment-failure alert

**Status:** accepted

**Context.** The brief asks for a streaming pipeline that reads business events from Kafka, groups them by time window
with state and a watermark, survives a crash without losing or repeating events, raises at least one real-time alert
visible on a dashboard, and monitors the flow (messages per second, lag, errors). Streaming costs more than batch, so it
must target the role where waiting hurts most. In the role table of the brief, payment (`service_account`) is rated
"very strong": a failed payment or a fraud spike must be seen before the order ends.

**Options.**
1. *Poll the database every few seconds.* Simple, but it loads PostgreSQL, has no history of events, and no replay.
2. *Kafka + a hand-written consumer.* Light, but windows, state, watermark and checkpoint would all be written by hand.
3. *Kafka + Spark Structured Streaming.* Windows, watermark, state and checkpoint are built in; same engine as the lakehouse (ADR-011).
Flink was considered and left aside: Spark and Delta are already in the project, so there is one engine to run and explain.

**Decision.** Option 3, with the order status events as the flow.
- **Events.** Orders publishes `order.status_changed` to topic `market.orders` (3 partitions, key = order id, so the events of one order stay in order) after each stored status change. Publishing is best effort and non-blocking: if Kafka is down, the order is not slowed or refused, and the error is logged. The producer is idempotent (`acks=all`).
- **Processing.** Spark reads Kafka every 10 s, drops duplicates by `event_id` inside a 2-minute watermark, counts orders per 1-minute window and per status, and raises a `payment_failure_spike` alert when a window holds 3 failed payments or more.
- **Delivery guarantee: exactly-once in effect.** Kafka to Spark is at-least-once. The checkpoint (Docker volume) stores the offsets and state, so after a crash Spark replays the last micro-batch; the deduplication by `event_id` and the idempotent Delta write (`txnAppId` + `txnVersion` = batch id) make that replay change nothing.
- **Outputs.** Window counts and alerts are appended to Delta tables `consumption/streaming/order_stats` and `alerts`. The alert is published to the Prometheus metrics before the Delta write, so Grafana shows it without waiting for storage.
- **Monitoring.** The job exposes `market_stream_*` metrics (messages per second, Kafka lag, batch duration, errors, alerts). The lag is computed by the job (latest Kafka offset minus processed offset), because Spark keeps its offsets in its checkpoint and not in a Kafka consumer group: Kafka UI therefore shows no consumer group for it.

**Consequences.**
- ➕ A crash and restart loses nothing and counts nothing twice (shown live: stop the job, send 4 failures, restart, the window shows 4).
- ➕ Orders does not depend on Kafka to work.
- ➖ Measured on the local S3 store, a micro-batch with two Delta writes takes about 5 s, so the "p99 < 2 s" target of the brief is not met end to end. The alert metric is published before the writes, which keeps the visible delay close to the 10 s trigger. *Plan B:* a shorter trigger, a faster store, or Kafka-only alerting with Delta written asynchronously.
- ➖ A window updated several times leaves several rows in Delta; the most recent `updated_at` is the valid one. *Plan B:* a periodic compaction or a view keeping the latest row.
- ➖ Fixed one-minute windows can split a burst of failures across two windows and hide a spike. *Plan B:* sliding windows (for example 1 minute every 10 s).
- ➖ One Kafka broker and the checkpoint on a local volume: no high availability. *Plan B:* 3 brokers with replication 3, and a checkpoint on shared storage.
- ➖ At-least-once publication from Orders: an event written to the database but lost before Kafka confirms is not replayed. *Plan B:* the outbox pattern (events saved in the same transaction, published by a relay).
