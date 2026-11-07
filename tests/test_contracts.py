import json
import sys
from pathlib import Path

import pytest
from graphql import build_schema

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import export_contracts  # noqa: E402


@pytest.mark.parametrize("service", export_contracts.REST)
def test_openapi_matches_committed_contract(service):
    committed = json.loads(export_contracts.path(service).read_text())
    assert export_contracts.openapi(service) == committed, "run: python scripts/export_contracts.py"


@pytest.mark.parametrize("service", export_contracts.REST)
def test_contract_is_versioned_and_secured(service):
    spec = export_contracts.openapi(service)
    assert all(p.startswith("/v1/") for p in spec["paths"])
    assert "HTTPBearer" in spec["components"]["securitySchemes"]


def test_graphql_contract_is_valid():
    schema = build_schema((export_contracts.SERVICES / "catalog" / "schema.graphql").read_text())
    assert {"createProduct", "updateProduct", "archiveProduct", "deleteProduct"} <= set(schema.mutation_type.fields)
