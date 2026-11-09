"""Inventory service — stock per warehouse, reservation / release / confirm (saga steps)."""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response
from psycopg import errors
from pydantic import BaseModel, Field

from common import observability
from common.auth import require_roles
from common.db import dsn_for, make_pool, ping

SATURATION_THRESHOLD = 0.90


class StockIn(BaseModel):
    warehouse_id: int
    quantity: int = Field(ge=0)


class Line(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0)


class ReservationIn(BaseModel):
    order_id: int = Field(gt=0)
    items: list[Line] = Field(min_length=1)


class ReservedLine(BaseModel):
    product_id: int
    warehouse_id: int
    quantity: int


class Reservation(BaseModel):
    order_id: int
    status: str  # reserved | released | confirmed
    items: list[ReservedLine]


class Warehouse(BaseModel):
    id: int
    name: str
    capacity_units: int
    stored_units: int
    fill_rate: float
    status: str  # normal | near_saturation


class ProductStock(BaseModel):
    product_id: int
    total: int
    warehouses: list[StockIn]


def _reservation(conn, order_id: int) -> dict | None:
    head = conn.execute("SELECT order_id, status FROM inventory.reservations WHERE order_id = %s",
                        (order_id,)).fetchone()
    if head:
        head["items"] = conn.execute(
            "SELECT product_id, warehouse_id, quantity FROM inventory.reservation_items "
            "WHERE order_id = %s ORDER BY product_id, warehouse_id", (order_id,)).fetchall()
    return head


def create_app(dsn: str | None = None) -> FastAPI:
    pool = make_pool(dsn or dsn_for("inventory_svc", "INVENTORY_DB_PASSWORD"))

    @asynccontextmanager
    async def lifespan(_):
        pool.open()
        yield
        pool.close()

    app = FastAPI(title="Market - Inventory", version="1.0.0", lifespan=lifespan)
    observability.setup(app, "inventory", lambda: ping(pool))
    saga = require_roles("admin", "service_account")

    @app.get("/v1/warehouses", response_model=list[Warehouse], tags=["warehouses"])
    def warehouses(_=Depends(require_roles("admin", "analyst", "logistics"))):
        with pool.connection() as conn:
            rows = conn.execute(
                """SELECT w.id, w.name, w.capacity_units, COALESCE(SUM(s.quantity), 0)::int AS stored_units
                   FROM inventory.warehouses w LEFT JOIN inventory.stock s ON s.warehouse_id = w.id
                   GROUP BY w.id ORDER BY w.id""").fetchall()
        for r in rows:
            r["fill_rate"] = round(r["stored_units"] / r["capacity_units"], 4) if r["capacity_units"] else 1.0
            r["status"] = "near_saturation" if r["fill_rate"] >= SATURATION_THRESHOLD else "normal"
        return rows

    @app.get("/v1/inventory/{product_id}", response_model=ProductStock, tags=["stock"])
    def get_stock(product_id: int, _=Depends(require_roles(
            "admin", "catalog_manager", "analyst", "logistics", "service_account"))):
        with pool.connection() as conn:
            rows = conn.execute("SELECT warehouse_id, quantity FROM inventory.stock WHERE product_id = %s "
                                "ORDER BY warehouse_id", (product_id,)).fetchall()
        return {"product_id": product_id, "total": sum(r["quantity"] for r in rows), "warehouses": rows}

    @app.put("/v1/inventory/{product_id}", response_model=ProductStock, tags=["stock"])
    def set_stock(product_id: int, body: StockIn,
                  _=Depends(require_roles("admin", "logistics", "service_account"))):
        """Sets the available quantity of a product in one warehouse (creates the line if needed)."""
        try:
            with pool.connection() as conn:
                conn.execute(
                    """INSERT INTO inventory.stock (product_id, warehouse_id, quantity) VALUES (%s, %s, %s)
                       ON CONFLICT (product_id, warehouse_id) DO UPDATE SET quantity = EXCLUDED.quantity""",
                    (product_id, body.warehouse_id, body.quantity))
        except errors.ForeignKeyViolation:
            raise HTTPException(404, "warehouse not found")
        return get_stock(product_id)

    @app.post("/v1/reservations", response_model=Reservation, status_code=201, tags=["reservations"],
              responses={200: {"description": "Already reserved (idempotent replay)"},
                         409: {"description": "Insufficient stock"}})
    def reserve(body: ReservationIn, response: Response, _=Depends(saga)):
        """All-or-nothing, idempotent on order_id: a replay never reserves twice."""
        wanted: dict[int, int] = {}
        for line in body.items:
            wanted[line.product_id] = wanted.get(line.product_id, 0) + line.quantity

        with pool.connection() as conn, conn.transaction():
            # The PK on order_id serialises concurrent replays: the 2nd insert waits, then conflicts.
            created = conn.execute("INSERT INTO inventory.reservations (order_id) VALUES (%s) "
                                   "ON CONFLICT DO NOTHING RETURNING order_id", (body.order_id,)).fetchone()
            if not created:
                response.status_code = 200
                return _reservation(conn, body.order_id)

            for product_id in sorted(wanted):  # fixed lock order -> no deadlock between orders
                rows = conn.execute(
                    "SELECT warehouse_id, quantity FROM inventory.stock WHERE product_id = %s AND quantity > 0 "
                    "ORDER BY quantity DESC, warehouse_id FOR UPDATE", (product_id,)).fetchall()
                missing = wanted[product_id]
                if sum(r["quantity"] for r in rows) < missing:
                    raise HTTPException(409, {"error": "insufficient_stock", "product_id": product_id})
                for r in rows:  # take from the fullest warehouses first
                    take = min(missing, r["quantity"])
                    conn.execute("UPDATE inventory.stock SET quantity = quantity - %s "
                                 "WHERE product_id = %s AND warehouse_id = %s",
                                 (take, product_id, r["warehouse_id"]))
                    conn.execute("INSERT INTO inventory.reservation_items VALUES (%s, %s, %s, %s)",
                                 (body.order_id, product_id, r["warehouse_id"], take))
                    missing -= take
                    if not missing:
                        break
            return _reservation(conn, body.order_id)

    def _transition(order_id: int, target: str) -> dict:
        with pool.connection() as conn, conn.transaction():
            current = conn.execute("SELECT status FROM inventory.reservations WHERE order_id = %s FOR UPDATE",
                                   (order_id,)).fetchone()
            if not current:
                raise HTTPException(404, "reservation not found")
            if current["status"] == target:  # idempotent
                return _reservation(conn, order_id)
            if current["status"] != "reserved":
                raise HTTPException(409, f"reservation already {current['status']}")
            if target == "released":  # compensation: give the stock back
                conn.execute(
                    """UPDATE inventory.stock s SET quantity = s.quantity + ri.quantity
                       FROM inventory.reservation_items ri
                       WHERE ri.order_id = %s AND s.product_id = ri.product_id
                         AND s.warehouse_id = ri.warehouse_id""", (order_id,))
            conn.execute("UPDATE inventory.reservations SET status = %s, updated_at = now() "
                         "WHERE order_id = %s", (target, order_id))
            return _reservation(conn, order_id)

    @app.get("/v1/reservations/{order_id}", response_model=Reservation, tags=["reservations"])
    def get_reservation(order_id: int, _=Depends(saga)):
        with pool.connection() as conn:
            res = _reservation(conn, order_id)
        if not res:
            raise HTTPException(404, "reservation not found")
        return res

    @app.post("/v1/reservations/{order_id}/release", response_model=Reservation, tags=["reservations"])
    def release(order_id: int, _=Depends(saga)):
        """Saga compensation: returns the reserved stock. Idempotent."""
        return _transition(order_id, "released")

    @app.post("/v1/reservations/{order_id}/confirm", response_model=Reservation, tags=["reservations"])
    def confirm(order_id: int, _=Depends(saga)):
        """Order paid: the reservation becomes final. Idempotent."""
        return _transition(order_id, "confirmed")

    return app

