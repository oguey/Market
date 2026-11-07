"""End-to-end: the real orders service calls the real catalog, inventory and payments services."""
import httpx
import pytest

from common.resilience import CircuitBreaker, ServiceClient
from conftest import token

SVC, ADMIN = token("service_account"), token("admin")


@pytest.fixture
def world(catalog, inventory, sql):
    """2 products in the catalog (one at 150 €, one at 250 €) with stock, and one customer."""
    create = "mutation($in: ProductInput!) { createProduct(input: $in) { productId } }"
    for pid, price in ((1, 15_000), (2, 25_000), (3, 1_000)):
        catalog.post("/v1/graphql", headers=token("catalog_manager"), json={"query": create, "variables": {
            "in": {"productId": pid, "name": f"P{pid}", "category": "test", "priceCents": price}}})
    catalog.post("/v1/graphql", headers=ADMIN, json={"query": "mutation { archiveProduct(productId: 3) { status } }"})
    sql("INSERT INTO inventory.warehouses (name, capacity_units) VALUES ('A', 100)")
    sql("INSERT INTO orders.customers (name, email) VALUES ('Léa', 'lea@example.com')")
    for pid in (1, 2):
        inventory.put(f"/v1/inventory/{pid}", json={"warehouse_id": 1, "quantity": 5}, headers=ADMIN)


def stock(inventory, pid):
    return inventory.get(f"/v1/inventory/{pid}", headers=SVC).json()["total"]


def order(client, items, key=None, headers=SVC):
    h = {**headers, **({"Idempotency-Key": key} if key else {})}
    return client.post("/v1/orders", json={"customer_id": 1, "items": items}, headers=h)


def test_paid_order(make_orders, world, inventory, payments):
    orders, _ = make_orders()
    resp = order(orders, [{"product_id": 1, "quantity": 1}])
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "paid" and body["total_cents"] == 15_000 and body["customer_name"] == "Léa"
    assert body["items"] == [{"product_id": 1, "quantity": 1, "unit_price_cents": 15_000}]
    assert stock(inventory, 1) == 4
    assert inventory.get(f"/v1/reservations/{body['id']}", headers=SVC).json()["status"] == "confirmed"
    assert payments.get(f"/v1/payments/{body['id']}", headers=SVC).json()["status"] == "succeeded"


def test_payment_refused_gives_stock_back(make_orders, world, inventory):
    orders, _ = make_orders()
    body = order(orders, [{"product_id": 2, "quantity": 1}]).json()  # 250 € > 200 € limit
    assert body["status"] == "payment_failed"
    assert stock(inventory, 2) == 5  # saga compensation
    assert inventory.get(f"/v1/reservations/{body['id']}", headers=SVC).json()["status"] == "released"


def test_out_of_stock_cancels(make_orders, world, inventory):
    orders, _ = make_orders()
    body = order(orders, [{"product_id": 1, "quantity": 6}]).json()
    assert body["status"] == "cancelled" and stock(inventory, 1) == 5


def test_idempotency_key_reserves_and_charges_once(make_orders, world, inventory, sql):
    orders, _ = make_orders()
    first = order(orders, [{"product_id": 1, "quantity": 1}], key="abc-123")
    again = order(orders, [{"product_id": 1, "quantity": 1}], key="abc-123")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"] and again.json()["status"] == "paid"
    assert stock(inventory, 1) == 4
    assert sql("SELECT count(*) FROM orders.orders") == [(1,)]


def test_unknown_or_archived_product_is_rejected_before_writing(make_orders, world, sql):
    orders, _ = make_orders()
    for pid in (99, 3):
        resp = order(orders, [{"product_id": pid, "quantity": 1}])
        assert resp.status_code == 422 and str(pid) in resp.json()["detail"]
    assert sql("SELECT count(*) FROM orders.orders") == [(0,)]


def test_unknown_customer_is_422(make_orders, world):
    orders, _ = make_orders()
    resp = orders.post("/v1/orders", json={"customer_id": 42, "items": [{"product_id": 1, "quantity": 1}]},
                       headers=SVC)
    assert resp.status_code == 422


def test_circuit_breaker_opens_when_inventory_is_down(make_orders, world, payments, sql):
    def down(request):
        raise httpx.ConnectError("inventory stopped")

    breaker = CircuitBreaker("inventory", failure_threshold=2, reset_timeout=60)
    dead = ServiceClient("inventory", "", backoff=0, breaker=breaker,
                         http=httpx.Client(transport=httpx.MockTransport(down), base_url="http://inventory"))
    orders, _ = make_orders(inventory=dead)

    resp = order(orders, [{"product_id": 1, "quantity": 1}])
    assert resp.status_code == 503 and resp.json()["status"] == "cancelled"
    assert breaker.state == "open"  # reserve + compensation both failed
    resp = order(orders, [{"product_id": 1, "quantity": 1}])
    assert resp.status_code == 503 and "circuit open" in resp.json()["detail"]
    assert sql("SELECT status FROM orders.orders") == [("cancelled",)]  # fail fast: 2nd order not even written
    assert payments.get("/v1/payments/1", headers=SVC).status_code == 404  # never charged
    assert "circuit_breaker_state" in orders.get("/metrics").text


def test_catalog_down_is_503_and_writes_nothing(make_orders, world, sql):
    dead = ServiceClient("catalog", "", backoff=0, retries=1,
                         http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)),
                                           base_url="http://catalog"))
    orders, _ = make_orders(catalog=dead)
    resp = order(orders, [{"product_id": 1, "quantity": 1}])
    assert resp.status_code == 503 and resp.json() == {"detail": "catalog unavailable: HTTP 500"}
    assert sql("SELECT count(*) FROM orders.orders") == [(0,)]


def test_roles_on_orders(make_orders, world):
    orders, _ = make_orders()
    assert order(orders, [{"product_id": 1, "quantity": 1}], headers=token("analyst")).status_code == 403
    assert orders.post("/v1/orders", json={"customer_id": 1, "items": [{"product_id": 1, "quantity": 1}]}
                       ).status_code == 401
    order(orders, [{"product_id": 1, "quantity": 1}], headers=ADMIN)
    assert len(orders.get("/v1/orders", headers=token("analyst")).json()) == 1
    assert orders.get("/v1/orders", headers=token("catalog_manager")).status_code == 403


def test_status_flow_after_payment(make_orders, world):
    orders, _ = make_orders()
    oid = order(orders, [{"product_id": 1, "quantity": 1}]).json()["id"]
    patch = lambda s: orders.patch(f"/v1/orders/{oid}/status", json={"status": s}, headers=ADMIN)
    assert patch("delivered").status_code == 409
    assert patch("preparing").json()["status"] == "preparing"
    assert patch("delivered").json()["status"] == "delivered"
    assert orders.get("/v1/orders?status=delivered", headers=ADMIN).json()[0]["id"] == oid
