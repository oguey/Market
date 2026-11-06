"""Orders service — customers and orders (PostgreSQL schema `orders`). Orchestrates the order saga."""
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from psycopg import errors
from pydantic import BaseModel, EmailStr, Field

from common import observability
from common.auth import ServiceToken, require_roles
from common.db import dsn_for, make_pool, ping
from common.resilience import DependencyError, ServiceClient
from orders.saga import Dependencies, OrderRejected, SagaFailed, place_order

READERS = ("admin", "analyst", "logistics", "service_account")
WRITERS = ("admin", "service_account")
# Allowed manual transitions after the saga (logistics flow).
NEXT_STATUS = {"paid": "preparing", "preparing": "delivered"}


class CustomerIn(BaseModel):
    name: str = Field(min_length=1)
    email: EmailStr


class Customer(CustomerIn):
    id: int


class OrderLineIn(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=1000)


class OrderIn(BaseModel):
    customer_id: int
    items: list[OrderLineIn] = Field(min_length=1, max_length=100)


class OrderLine(BaseModel):
    product_id: int
    quantity: int
    unit_price_cents: int


class Order(BaseModel):
    id: int
    customer_id: int
    customer_name: str
    status: str  # pending | paid | payment_failed | cancelled | preparing | delivered
    total_cents: int
    created_at: datetime
    items: list[OrderLine] = []


class StatusIn(BaseModel):
    status: Literal["preparing", "delivered"]


def default_dependencies() -> Dependencies:
    token = ServiceToken("orders-svc")

    def client(name: str, env: str, default: str) -> ServiceClient:
        return ServiceClient(name, os.getenv(env, default), token=token)

    return Dependencies(catalog=client("catalog", "CATALOG_URL", "http://localhost:8004"),
                        inventory=client("inventory", "INVENTORY_URL", "http://localhost:8002"),
                        payments=client("payments", "PAYMENTS_URL", "http://localhost:8003"))


ORDER_SQL = """SELECT o.id, o.customer_id, c.name AS customer_name, o.status, o.total_cents, o.created_at
               FROM orders.orders o JOIN orders.customers c ON c.id = o.customer_id"""


def create_app(dsn: str | None = None, deps: Dependencies | None = None) -> FastAPI:
    pool = make_pool(dsn or dsn_for("orders_svc", "ORDERS_DB_PASSWORD"))
    deps = deps or default_dependencies()

    @asynccontextmanager
    async def lifespan(_):
        pool.open()
        yield
        pool.close()

    app = FastAPI(title="Market - Orders", version="1.0.0", lifespan=lifespan)
    observability.setup(app, "orders", lambda: ping(pool))

    def load_order(order_id: int) -> dict:
        with pool.connection() as conn:
            order = conn.execute(ORDER_SQL + " WHERE o.id = %s", (order_id,)).fetchone()
            if order:
                order["items"] = conn.execute(
                    "SELECT product_id, quantity, unit_price_cents FROM orders.order_items "
                    "WHERE order_id = %s ORDER BY id", (order_id,)).fetchall()
        if not order:
            raise HTTPException(404, "order not found")
        return order

    @app.post("/v1/customers", response_model=Customer, status_code=201, tags=["customers"])
    def create_customer(body: CustomerIn, _=Depends(require_roles(*WRITERS))):
        try:
            with pool.connection() as conn:
                return conn.execute("INSERT INTO orders.customers (name, email) VALUES (%s, %s) "
                                    "RETURNING id, name, email", (body.name, body.email)).fetchone()
        except errors.UniqueViolation:
            raise HTTPException(409, "email already used")

    @app.post("/v1/orders", response_model=Order, status_code=201, tags=["orders"],
              responses={200: {"description": "Idempotent replay (same Idempotency-Key)"},
                         422: {"description": "Unknown customer or product"},
                         503: {"description": "A dependency is unavailable; order compensated"}})
    def create_order(body: OrderIn, response: Response,
                     idempotency_key: str | None = Header(None, max_length=128),
                     _=Depends(require_roles(*WRITERS))):
        """Runs the saga. The final `status` tells the outcome: paid, payment_failed or cancelled (no stock)."""
        try:
            order, created = place_order(pool, deps, load_order, body.customer_id,
                                         [i.model_dump() for i in body.items], idempotency_key)
        except OrderRejected as exc:
            raise HTTPException(422, str(exc))
        except SagaFailed as exc:  # order saved, then compensated
            return JSONResponse(status_code=503, content={
                "detail": str(exc.cause), "order_id": exc.order["id"], "status": exc.order["status"]})
        except DependencyError as exc:  # nothing was written
            return JSONResponse(status_code=503, content={"detail": str(exc)})
        if not created:
            response.status_code = 200
        return order

    @app.get("/v1/orders", response_model=list[Order], tags=["orders"])
    def list_orders(limit: int = Query(50, ge=1, le=200), status: str | None = None,
                    _=Depends(require_roles(*READERS))):
        """Most recent first, without lines (use GET /v1/orders/{id} for details)."""
        sql, params = ORDER_SQL, []
        if status:
            sql += " WHERE o.status = %s"
            params.append(status)
        with pool.connection() as conn:
            return conn.execute(sql + " ORDER BY o.created_at DESC, o.id DESC LIMIT %s",
                                (*params, limit)).fetchall()

    @app.get("/v1/orders/{order_id}", response_model=Order, tags=["orders"])
    def get_order(order_id: int, _=Depends(require_roles(*READERS))):
        return load_order(order_id)

    @app.patch("/v1/orders/{order_id}/status", response_model=Order, tags=["orders"])
    def update_status(order_id: int, body: StatusIn, _=Depends(require_roles("admin"))):
        current = load_order(order_id)["status"]
        if NEXT_STATUS.get(current) != body.status:
            raise HTTPException(409, f"cannot go from {current} to {body.status}")
        with pool.connection() as conn:
            conn.execute("UPDATE orders.orders SET status = %s WHERE id = %s AND status = %s",
                         (body.status, order_id, current))
        return load_order(order_id)

    return app

