from conftest import token

SVC = token("service_account")


def test_payment_under_limit_succeeds(payments):
    resp = payments.post("/v1/payments", json={"order_id": 1, "amount_cents": 20_000}, headers=SVC)
    assert resp.status_code == 201 and resp.json()["status"] == "succeeded"


def test_payment_over_200_euros_is_refused(payments):
    resp = payments.post("/v1/payments", json={"order_id": 2, "amount_cents": 20_001}, headers=SVC)
    assert resp.status_code == 201 and resp.json()["status"] == "failed"


def test_payment_is_idempotent(payments, sql):
    first = payments.post("/v1/payments", json={"order_id": 3, "amount_cents": 500}, headers=SVC)
    again = payments.post("/v1/payments", json={"order_id": 3, "amount_cents": 500}, headers=SVC)
    assert again.status_code == 200 and again.json() == first.json()
    assert sql("SELECT count(*) FROM payments.payments WHERE order_id = 3") == [(1,)]


def test_same_order_different_amount_is_409(payments):
    payments.post("/v1/payments", json={"order_id": 4, "amount_cents": 500}, headers=SVC)
    assert payments.post("/v1/payments", json={"order_id": 4, "amount_cents": 900}, headers=SVC).status_code == 409


def test_negative_amount_is_422(payments):
    assert payments.post("/v1/payments", json={"order_id": 5, "amount_cents": -1}, headers=SVC).status_code == 422


def test_analyst_cannot_read_payments(payments):
    assert payments.get("/v1/payments/1", headers=token("analyst")).status_code == 403


def test_health_and_metrics(payments):
    assert payments.get("/health").json() == {"status": "ok"}
    assert "http_requests_total" in payments.get("/metrics").text
