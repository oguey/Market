"""Payments service — charges an order. Business rule from the brief: refuse above 200 €."""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from common import observability
from common.auth import require_roles
from common.db import dsn_for, make_pool, ping

MAX_AMOUNT_CENTS = 20_000  # 200 €


class PaymentIn(BaseModel):
    order_id: int = Field(gt=0)
    amount_cents: int = Field(ge=0)


class Payment(BaseModel):
    id: int
    order_id: int
    amount_cents: int
    status: str  # succeeded | failed


def create_app(dsn: str | None = None) -> FastAPI:
    pool = make_pool(dsn or dsn_for("payments_svc", "PAYMENTS_DB_PASSWORD"))

    @asynccontextmanager
    async def lifespan(_):
        pool.open()
        yield
        pool.close()

    app = FastAPI(title="Market - Payments", version="1.0.0", lifespan=lifespan)
    observability.setup(app, "payments", lambda: ping(pool))
    writer = require_roles("admin", "service_account")

    @app.post("/v1/payments", response_model=Payment, status_code=201, tags=["payments"],
              responses={200: {"description": "Already processed (idempotent replay)"},
                         409: {"description": "order_id already paid with another amount"}})
    def pay(body: PaymentIn, response: Response, _=Depends(writer)):
        """Idempotent on order_id: calling twice returns the first result, never charges twice."""
        status = "failed" if body.amount_cents > MAX_AMOUNT_CENTS else "succeeded"
        with pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO payments.payments (order_id, amount_cents, status) VALUES (%s, %s, %s)
                   ON CONFLICT (order_id) DO NOTHING
                   RETURNING id, order_id, amount_cents, status""",
                (body.order_id, body.amount_cents, status)).fetchone()
            if row:
                return row
            row = conn.execute("SELECT id, order_id, amount_cents, status FROM payments.payments "
                               "WHERE order_id = %s", (body.order_id,)).fetchone()
        if row["amount_cents"] != body.amount_cents:
            raise HTTPException(409, "order already paid with a different amount")
        response.status_code = 200
        return row

    @app.get("/v1/payments/{order_id}", response_model=Payment, tags=["payments"])
    def get_payment(order_id: int, _=Depends(writer)):
        with pool.connection() as conn:
            row = conn.execute("SELECT id, order_id, amount_cents, status FROM payments.payments "
                               "WHERE order_id = %s", (order_id,)).fetchone()
        if not row:
            raise HTTPException(404, "payment not found")
        return row

    return app

