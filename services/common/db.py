"""PostgreSQL connection pool. Each service logs in with its *own* role, so it can only see its schema."""
import os

from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def dsn_for(role: str, password_env: str) -> str:
    return make_conninfo(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "market"),
        user=role,
        password=os.getenv(password_env, ""),
    )


def make_pool(dsn: str) -> ConnectionPool:
    return ConnectionPool(dsn, min_size=1, max_size=10, open=False, kwargs={"row_factory": dict_row})


def ping(pool: ConnectionPool) -> None:
    with pool.connection(timeout=2) as conn:
        conn.execute("SELECT 1")
