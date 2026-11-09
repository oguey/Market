"""Volet 1 rule: each service has its own PostgreSQL role and can never read another service's tables."""
import psycopg
import pytest

OWN = {"orders_svc": "orders.orders", "inventory_svc": "inventory.stock", "payments_svc": "payments.payments"}


@pytest.mark.parametrize("role", OWN)
def test_service_reads_only_its_schema(pg, role):
    with psycopg.connect(pg["role"](role), autocommit=True) as conn:
        conn.execute(f"SELECT 1 FROM {OWN[role]} LIMIT 1")
        for other_role, table in OWN.items():
            if other_role != role:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
