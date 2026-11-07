import pytest

from conftest import token

SVC, LOG = token("service_account"), token("logistics")


@pytest.fixture
def stock(inventory, sql):
    sql("INSERT INTO inventory.warehouses (name, capacity_units) VALUES ('A', 100), ('B', 50)")
    inventory.put("/v1/inventory/1", json={"warehouse_id": 1, "quantity": 3}, headers=LOG)
    inventory.put("/v1/inventory/1", json={"warehouse_id": 2, "quantity": 2}, headers=LOG)
    inventory.put("/v1/inventory/2", json={"warehouse_id": 1, "quantity": 10}, headers=LOG)


def total(inventory, product_id):
    return inventory.get(f"/v1/inventory/{product_id}", headers=SVC).json()["total"]


def test_set_and_read_stock(inventory, stock):
    body = inventory.get("/v1/inventory/1", headers=token("catalog_manager")).json()
    assert body["total"] == 5 and len(body["warehouses"]) == 2


def test_unknown_warehouse_is_404(inventory, stock):
    assert inventory.put("/v1/inventory/1", json={"warehouse_id": 99, "quantity": 1}, headers=LOG).status_code == 404


def test_reservation_spans_warehouses(inventory, stock):
    resp = inventory.post("/v1/reservations", json={"order_id": 10, "items": [{"product_id": 1, "quantity": 4}]},
                          headers=SVC)
    assert resp.status_code == 201
    assert sum(i["quantity"] for i in resp.json()["items"]) == 4 and len(resp.json()["items"]) == 2
    assert total(inventory, 1) == 1


def test_insufficient_stock_reserves_nothing(inventory, stock):
    resp = inventory.post("/v1/reservations", headers=SVC, json={
        "order_id": 11, "items": [{"product_id": 2, "quantity": 1}, {"product_id": 1, "quantity": 6}]})
    assert resp.status_code == 409 and resp.json()["detail"]["product_id"] == 1
    assert total(inventory, 2) == 10  # all-or-nothing: product 2 untouched
    assert inventory.get("/v1/reservations/11", headers=SVC).status_code == 404


def test_reservation_is_idempotent(inventory, stock):
    body = {"order_id": 12, "items": [{"product_id": 2, "quantity": 3}]}
    assert inventory.post("/v1/reservations", json=body, headers=SVC).status_code == 201
    assert inventory.post("/v1/reservations", json=body, headers=SVC).status_code == 200
    assert total(inventory, 2) == 7  # reserved once only


def test_release_gives_stock_back_once(inventory, stock):
    inventory.post("/v1/reservations", json={"order_id": 13, "items": [{"product_id": 1, "quantity": 5}]}, headers=SVC)
    assert total(inventory, 1) == 0
    for _ in range(2):  # idempotent compensation
        assert inventory.post("/v1/reservations/13/release", headers=SVC).json()["status"] == "released"
    assert total(inventory, 1) == 5
    assert inventory.post("/v1/reservations/13/confirm", headers=SVC).status_code == 409


def test_release_unknown_is_404(inventory):
    assert inventory.post("/v1/reservations/999/release", headers=SVC).status_code == 404


def test_warehouse_fill_rate_and_saturation(inventory, stock):
    inventory.put("/v1/inventory/3", json={"warehouse_id": 2, "quantity": 43}, headers=LOG)
    rows = {w["name"]: w for w in inventory.get("/v1/warehouses", headers=token("analyst")).json()}
    assert rows["A"]["fill_rate"] == 0.13 and rows["A"]["status"] == "normal"
    assert rows["B"]["fill_rate"] == 0.9 and rows["B"]["status"] == "near_saturation"


def test_roles(inventory, stock):
    assert inventory.get("/v1/warehouses", headers=SVC).status_code == 403
    assert inventory.put("/v1/inventory/1", json={"warehouse_id": 1, "quantity": 1},
                         headers=token("analyst")).status_code == 403
    assert inventory.post("/v1/reservations", json={"order_id": 1, "items": [{"product_id": 1, "quantity": 1}]},
                          headers=LOG).status_code == 403
