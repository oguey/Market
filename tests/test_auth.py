import jwt
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from common.auth import create_token, require_roles
from conftest import token

app = FastAPI()


@app.get("/v1/orders")
def orders(user=Depends(require_roles("admin", "analyst"))):
    return {"sub": user["sub"]}


client = TestClient(app)


def test_no_token_is_401():
    assert client.get("/v1/orders").status_code == 401


def test_expired_token_is_401():
    resp = client.get("/v1/orders", headers=token("admin", ttl=-10))
    assert resp.status_code == 401 and resp.json()["detail"] == "token expired"


def test_forged_token_is_401():
    forged = jwt.encode({"sub": "x", "roles": ["admin"], "exp": 9999999999}, "a-different-key-that-is-long-enough-32b", algorithm="HS256")
    assert client.get("/v1/orders", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_wrong_role_is_403_and_logged(caplog):
    assert client.get("/v1/orders", headers=token("catalog_manager")).status_code == 403
    assert "access denied status=403 user=test-catalog_manager action=GET /v1/orders" in caplog.text


def test_right_role_is_200():
    assert client.get("/v1/orders", headers=token("analyst")).json() == {"sub": "test-analyst"}


def test_token_contains_no_personal_data():
    claims = jwt.decode(create_token("42", ["analyst"]), options={"verify_signature": False})
    assert set(claims) == {"sub", "roles", "iat", "exp"}
