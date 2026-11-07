"""Regenerate the versioned OpenAPI contracts: python scripts/export_contracts.py

The catalog contract is services/catalog/schema.graphql (schema-first, edited by hand).
tests/test_contracts.py fails if code and committed contracts drift apart.
"""
import importlib
import json
import sys
from pathlib import Path

SERVICES = Path(__file__).resolve().parent.parent / "services"
sys.path.insert(0, str(SERVICES))
REST = ("orders", "inventory", "payments")


def openapi(service: str) -> dict:
    return importlib.import_module(f"{service}.main").create_app(dsn="postgresql://unused").openapi()


def path(service: str) -> Path:
    return SERVICES / service / "openapi.json"


if __name__ == "__main__":
    for svc in REST:
        path(svc).write_text(json.dumps(openapi(svc), indent=2, ensure_ascii=False) + "\n")
        print("wrote", path(svc))
