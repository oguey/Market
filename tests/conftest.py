"""Test fixtures: a real PostgreSQL 16 and a real MongoDB 7, started in-process (no Docker).

The *real* init scripts from db/ are applied, and each service connects with its *own* role,
so schema isolation is tested too.
"""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import pgserver
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo

os.environ.setdefault("JWT_SECRET", "test-secret-not-for-prod-0123456789abcdef")
# In-process MongoDB 7 on a non-default port, so it never collides with a docker-compose Mongo.
os.environ.setdefault("PYMONGOIM__MONGO_VERSION", "7.0")
os.environ.setdefault("PYMONGOIM__MONGOD_PORT", "27099")
os.environ.setdefault("PYMONGOIM__OPERATING_SYSTEM", "ubuntu")
os.environ.setdefault("PYMONGOIM__OS_VERSION", "22")

from common.auth import ServiceToken, create_token  # noqa: E402
from common.resilience import CircuitBreaker, ServiceClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PG_INIT = ROOT / "db" / "postgres" / "init"
MONGO_INIT = ROOT / "db" / "mongo" / "init" / "01_collections.js"


def token(*roles: str, ttl: int = 900) -> dict:
    return {"Authorization": f"Bearer {create_token('test-' + '-'.join(roles), list(roles), ttl)}"}


# ---------------------------------------------------------------- PostgreSQL
@pytest.fixture(scope="session")
def pg():
    datadir = tempfile.mkdtemp(prefix="market-pg-")
    server = pgserver.get_server(datadir, cleanup_mode="stop")
    base = conninfo_to_dict(server.get_uri())
    with psycopg.connect(make_conninfo(**base), autocommit=True) as conn:
        conn.execute("CREATE DATABASE market")
    admin_dsn = make_conninfo(**{**base, "dbname": "market"})
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        for sql in ("01_schemas.sql", "02_tables.sql", "03_service_tables.sql"):
            conn.execute((PG_INIT / sql).read_text())
        roles_sql = re.search(r"<<EOSQL\n(.*)EOSQL", (PG_INIT / "05_roles.sh").read_text(), re.S).group(1)
        conn.execute(re.sub(r"\$\{(\w+)\}", "test", roles_sql))
    yield {"admin": admin_dsn, "role": lambda r: make_conninfo(**{**base, "dbname": "market", "user": r})}
    server.cleanup()
    shutil.rmtree(datadir, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_pg(request):
    yield
    if "pg" in request.fixturenames:
        with psycopg.connect(request.getfixturevalue("pg")["admin"], autocommit=True) as conn:
            conn.execute("""TRUNCATE orders.order_items, orders.orders, orders.customers,
                            inventory.reservation_items, inventory.reservations, inventory.stock,
                            inventory.warehouses, payments.payments RESTART IDENTITY CASCADE""")


@pytest.fixture
def sql(pg):
    """Run SQL as the superuser (test setup / assertions only)."""
    def run(query: str, params=()):
        with psycopg.connect(pg["admin"], autocommit=True) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else None
    return run


# ---------------------------------------------------------------- MongoDB
@pytest.fixture(scope="session")
def mongo_db():
    from pymongo_inmemory import MongoClient
    client = MongoClient()
    yield client["market"]
    client.close()


def _validator_from_init_script() -> dict:
    """Reuse the $jsonSchema validator of the real init script (JS literal -> JSON)."""
    js = MONGO_INIT.read_text()
    body = js[js.index("validator:") + len("validator:"):js.index("});")]
    body = body[:body.rindex("}") + 1]
    return json.loads(re.sub(r"([\{,]\s*)(\$?\w+)\s*:", r'\1"\2":', body))


@pytest.fixture
def catalog_collection(mongo_db):
    mongo_db.drop_collection("catalog")
    mongo_db.create_collection("catalog", validator=_validator_from_init_script())
    return mongo_db["catalog"]


# ---------------------------------------------------------------- Services
@pytest.fixture
def payments(pg):
    from payments.main import create_app
    with TestClient(create_app(pg["role"]("payments_svc"))) as client:
        yield client


@pytest.fixture
def inventory(pg):
    from inventory.main import create_app
    with TestClient(create_app(pg["role"]("inventory_svc"))) as client:
        yield client


@pytest.fixture
def catalog(catalog_collection):
    from catalog.main import create_app
    with TestClient(create_app(catalog_collection)) as client:
        yield client


def in_process_client(name: str, app_client: TestClient, **kwargs) -> ServiceClient:
    return ServiceClient(name, "", http=app_client, token=ServiceToken("orders-svc"), backoff=0,
                         breaker=CircuitBreaker(name, failure_threshold=3, reset_timeout=60), **kwargs)


@pytest.fixture
def make_orders(pg, catalog, inventory, payments):
    """Builds the orders service wired to the 3 other *real* services (in-process)."""
    from orders.main import create_app
    from orders.saga import Dependencies
    opened = []

    def build(**overrides):
        deps = Dependencies(catalog=in_process_client("catalog", catalog),
                            inventory=in_process_client("inventory", inventory),
                            payments=in_process_client("payments", payments))
        for name, client in overrides.items():
            setattr(deps, name, client)
        client = TestClient(create_app(pg["role"]("orders_svc"), deps))
        client.__enter__()
        opened.append(client)
        return client, deps

    yield build
    for c in opened:
        c.__exit__(None, None, None)
