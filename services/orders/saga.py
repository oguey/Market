"""Order saga (orchestrated): price -> save -> reserve stock -> pay -> confirm, with compensation.

    catalog.products(ids)          prices + product_id check (no FK across services)
    INSERT order (pending)         one transaction: order + all its lines, or nothing
    inventory reserve              409 -> order "cancelled"
    payments pay                   failed -> inventory release (compensation) -> "payment_failed"
    inventory confirm              -> "paid"

Every downstream call is idempotent on order_id, so retries and replays never double-book.
"""
import logging
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from common.resilience import DependencyError, ServiceClient

log = logging.getLogger("saga")

PRODUCTS_QUERY = "query($ids: [Int!]) { products(ids: $ids, limit: 200) { productId priceCents status } }"


class OrderRejected(Exception):
    """Business refusal detected before anything was written (unknown product, ...)."""


class SagaFailed(Exception):
    """A dependency was unavailable; the order was saved then compensated."""

    def __init__(self, order: dict, cause: DependencyError):
        super().__init__(str(cause))
        self.order, self.cause = order, cause


@dataclass
class Dependencies:
    catalog: ServiceClient
    inventory: ServiceClient
    payments: ServiceClient


def fetch_prices(catalog: ServiceClient, product_ids: list[int]) -> dict[int, int]:
    resp = catalog.request("POST", "/v1/graphql", json={"query": PRODUCTS_QUERY, "variables": {"ids": product_ids}})
    body = resp.json()
    if resp.status_code != 200 or body.get("errors"):
        raise DependencyError("catalog", f"unexpected response {resp.status_code}: {body.get('errors')}")
    prices = {p["productId"]: p["priceCents"] for p in body["data"]["products"] if p["status"] == "active"}
    unknown = sorted(set(product_ids) - prices.keys())
    if unknown:
        raise OrderRejected(f"unknown or archived products: {unknown}")
    return prices


def _set_status(pool: ConnectionPool, order_id: int, status: str) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE orders.orders SET status = %s WHERE id = %s", (status, order_id))


def _release(deps: Dependencies, order_id: int) -> None:
    """Compensation. Best effort: if inventory is down, the reservation stays and is logged for replay."""
    try:
        resp = deps.inventory.request("POST", f"/v1/reservations/{order_id}/release")
        if resp.status_code not in (200, 404):
            log.error("release order=%s unexpected status %s", order_id, resp.status_code)
    except DependencyError as exc:
        log.error("COMPENSATION PENDING order=%s release failed: %s", order_id, exc)


def place_order(pool: ConnectionPool, deps: Dependencies, load_order, customer_id: int,
                items: list[dict], idempotency_key: str | None) -> tuple[dict, bool]:
    """Returns (order, created). created=False means an idempotent replay."""
    if idempotency_key:
        with pool.connection() as conn:
            row = conn.execute("SELECT id FROM orders.orders WHERE idempotency_key = %s",
                               (idempotency_key,)).fetchone()
        if row:
            return load_order(row["id"]), False

    for dep in (deps.inventory, deps.payments):  # fail fast: don't write an order that can't complete
        if dep.breaker.state == dep.breaker.OPEN:
            raise DependencyError(dep.name, "circuit open")
    prices = fetch_prices(deps.catalog, sorted({i["product_id"] for i in items}))
    total = sum(prices[i["product_id"]] * i["quantity"] for i in items)

    with pool.connection() as conn, conn.transaction():
        if not conn.execute("SELECT 1 FROM orders.customers WHERE id = %s", (customer_id,)).fetchone():
            raise OrderRejected(f"customer {customer_id} not found")
        row = conn.execute(
            """INSERT INTO orders.orders (customer_id, status, total_cents, idempotency_key)
               VALUES (%s, 'pending', %s, %s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id""",
            (customer_id, total, idempotency_key)).fetchone()
        if row is None:  # concurrent request with the same key won the race
            existing = conn.execute("SELECT id FROM orders.orders WHERE idempotency_key = %s",
                                    (idempotency_key,)).fetchone()
            return load_order(existing["id"]), False
        order_id = row["id"]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO orders.order_items (order_id, product_id, quantity, unit_price_cents) "
                "VALUES (%s, %s, %s, %s)",
                [(order_id, i["product_id"], i["quantity"], prices[i["product_id"]]) for i in items])

    # 1) reserve stock
    try:
        resp = deps.inventory.request("POST", "/v1/reservations", json={"order_id": order_id, "items": items})
    except DependencyError as exc:
        _release(deps, order_id)  # the reservation may have succeeded before the timeout
        _set_status(pool, order_id, "cancelled")
        raise SagaFailed(load_order(order_id), exc)
    if resp.status_code == 409:
        _set_status(pool, order_id, "cancelled")
        return load_order(order_id), True
    if resp.status_code not in (200, 201):
        _set_status(pool, order_id, "cancelled")
        raise SagaFailed(load_order(order_id), DependencyError("inventory", f"HTTP {resp.status_code}"))

    # 2) pay
    try:
        resp = deps.payments.request("POST", "/v1/payments", json={"order_id": order_id, "amount_cents": total})
        paid = resp.status_code in (200, 201) and resp.json()["status"] == "succeeded"
    except DependencyError as exc:
        _release(deps, order_id)
        _set_status(pool, order_id, "payment_failed")
        raise SagaFailed(load_order(order_id), exc)
    if not paid:
        _release(deps, order_id)
        _set_status(pool, order_id, "payment_failed")
        return load_order(order_id), True

    # 3) confirm (stock already decremented; a failed confirm leaves it "reserved", which is harmless)
    try:
        deps.inventory.request("POST", f"/v1/reservations/{order_id}/confirm")
    except DependencyError as exc:
        log.warning("confirm order=%s failed (stock stays reserved): %s", order_id, exc)
    _set_status(pool, order_id, "paid")
    return load_order(order_id), True
