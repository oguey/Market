import bcrypt
import jwt
import pytest
from fastapi.testclient import TestClient

from common.auth import decode_token
from conftest import token

PASSWORD = "S3cret-pass"


@pytest.fixture(autouse=True)
def clean_auth(pg, sql):
    sql("TRUNCATE auth.user_roles, auth.users, auth.roles RESTART IDENTITY CASCADE")
    sql("""INSERT INTO auth.roles (name) VALUES
           ('admin'), ('catalog_manager'), ('analyst'), ('logistics'), ('service_account')""")


@pytest.fixture
def auth(pg):
    from auth.main import create_app
    with TestClient(create_app(pg["role"]("auth_svc"))) as client:
        yield client


@pytest.fixture
def add_user(sql):
    def add(email, roles, password=PASSWORD, active=True):
        h = bcrypt.hashpw(password.encode(), bcrypt.gensalt(4)).decode()
        uid = sql("INSERT INTO auth.users (email, password_hash, is_active) VALUES (%s, %s, %s) RETURNING id",
                  (email, h, active))[0][0]
        for r in roles:
            sql("INSERT INTO auth.user_roles SELECT %s, id FROM auth.roles WHERE name = %s", (uid, r))
        return uid
    return add


def login(auth, email, password=PASSWORD):
    return auth.post("/v1/auth/login", json={"email": email, "password": password})


def test_login_returns_jwt_with_sub_roles_exp_and_no_email(auth, add_user):
    uid = add_user("lea@market.test", ["analyst"])
    resp = login(auth, "lea@market.test")
    assert resp.status_code == 200
    body = resp.json()
    claims = decode_token(body["access_token"])
    assert claims["sub"] == str(uid) and claims["roles"] == ["analyst"]
    assert claims["exp"] - claims["iat"] == 900
    assert "email" not in claims and "lea@market.test" not in body["access_token"]
    assert body["expires_in"] == 900 and body["token_type"] == "bearer"


def test_user_with_two_roles_gets_both(auth, add_user):
    add_user("multi@market.test", ["logistics", "analyst"])
    assert login(auth, "multi@market.test").json()["roles"] == ["analyst", "logistics"]


def test_email_is_case_insensitive(auth, add_user):
    add_user("lea@market.test", ["analyst"])
    assert login(auth, "  LEA@Market.test ").status_code == 200


@pytest.mark.parametrize("email,password", [("lea@market.test", "wrong"), ("nobody@market.test", PASSWORD)])
def test_bad_credentials_are_401_with_same_message(auth, add_user, caplog, email, password):
    add_user("lea@market.test", ["analyst"])
    resp = login(auth, email, password)
    assert resp.status_code == 401 and resp.json()["detail"] == "invalid credentials"
    assert f"login failed email={email}" in caplog.text


def test_disabled_account_and_user_without_role_are_refused(auth, add_user):
    add_user("off@market.test", ["analyst"], active=False)
    add_user("norole@market.test", [])
    assert login(auth, "off@market.test").status_code == 401
    assert login(auth, "norole@market.test").status_code == 401


def test_password_is_stored_as_bcrypt_hash(auth, sql):
    admin = token("admin")
    auth.post("/v1/auth/users", json={"email": "new@market.test", "password": PASSWORD, "roles": ["analyst"]},
              headers=admin)
    stored = sql("SELECT password_hash FROM auth.users WHERE email = 'new@market.test'")[0][0]
    assert stored.startswith("$2") and PASSWORD not in stored


def test_seed_style_pgcrypto_hash_can_log_in(auth, sql):
    """The hashes made by db/postgres/init/06_seed_users.sh (pgcrypto) must be accepted."""
    sql("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    sql("INSERT INTO auth.users (email, password_hash) VALUES ('seed@market.test', crypt(%s, gen_salt('bf', 6)))",
        (PASSWORD,))
    sql("INSERT INTO auth.user_roles SELECT u.id, r.id FROM auth.users u, auth.roles r "
        "WHERE u.email = 'seed@market.test' AND r.name = 'admin'")
    assert login(auth, "seed@market.test").status_code == 200


def test_refresh_gives_new_access_token_with_current_roles(auth, add_user, sql):
    uid = add_user("lea@market.test", ["analyst"])
    refresh = login(auth, "lea@market.test").json()["refresh_token"]
    sql("INSERT INTO auth.user_roles SELECT %s, id FROM auth.roles WHERE name = 'logistics'", (uid,))
    resp = auth.post("/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    assert decode_token(resp.json()["access_token"])["roles"] == ["analyst", "logistics"]


def test_access_token_cannot_be_used_to_refresh(auth, add_user):
    add_user("lea@market.test", ["analyst"])
    access = login(auth, "lea@market.test").json()["access_token"]
    assert auth.post("/v1/auth/refresh", json={"refresh_token": access}).status_code == 401
    assert auth.post("/v1/auth/refresh", json={"refresh_token": "garbage"}).status_code == 401


def test_refresh_token_cannot_be_used_as_access_token(auth, add_user):
    add_user("lea@market.test", ["analyst"])
    refresh = login(auth, "lea@market.test").json()["refresh_token"]
    assert auth.get("/v1/auth/me", headers={"Authorization": f"Bearer {refresh}"}).status_code == 401


def test_refresh_refused_after_account_disabled(auth, add_user, sql):
    uid = add_user("lea@market.test", ["analyst"])
    refresh = login(auth, "lea@market.test").json()["refresh_token"]
    sql("UPDATE auth.users SET is_active = false WHERE id = %s", (uid,))
    assert auth.post("/v1/auth/refresh", json={"refresh_token": refresh}).status_code == 401


def test_me_needs_a_token_and_returns_it(auth, add_user):
    assert auth.get("/v1/auth/me").status_code == 401
    add_user("lea@market.test", ["analyst"])
    access = login(auth, "lea@market.test").json()["access_token"]
    resp = auth.get("/v1/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert resp.json()["roles"] == ["analyst"]


def test_only_admin_can_create_users(auth):
    payload = {"email": "x@market.test", "password": PASSWORD, "roles": ["analyst"]}
    assert auth.post("/v1/auth/users", json=payload).status_code == 401
    assert auth.post("/v1/auth/users", json=payload, headers=token("analyst")).status_code == 403
    assert auth.post("/v1/auth/users", json=payload, headers=token("admin")).status_code == 201


def test_created_user_can_log_in_and_duplicates_are_409(auth):
    payload = {"email": "New@market.test", "password": PASSWORD, "roles": ["logistics"]}
    created = auth.post("/v1/auth/users", json=payload, headers=token("admin")).json()
    assert created["email"] == "new@market.test" and created["roles"] == ["logistics"]
    assert login(auth, "new@market.test").status_code == 200
    assert auth.post("/v1/auth/users", json=payload, headers=token("admin")).status_code == 409


def test_create_user_validation(auth, sql):
    admin = token("admin")
    base = {"email": "v@market.test", "password": PASSWORD, "roles": ["analyst"]}
    assert auth.post("/v1/auth/users", json={**base, "roles": ["superhero"]}, headers=admin).status_code == 422
    assert auth.post("/v1/auth/users", json={**base, "password": "short"}, headers=admin).status_code == 422
    assert auth.post("/v1/auth/users", json={**base, "email": "not-an-email"}, headers=admin).status_code == 422
    sql("DELETE FROM auth.roles WHERE name = 'logistics'")  # role valid in code, missing in the table
    assert auth.post("/v1/auth/users", json={**base, "roles": ["logistics"]}, headers=admin).status_code == 422
    assert sql("SELECT count(*) FROM auth.users")[0][0] == 0  # rolled back, no half-created user


def test_token_from_login_is_accepted_by_another_service(auth, add_user, inventory):
    """End to end: the JWT issued here passes the RBAC check of a different service."""
    add_user("admin@market.test", ["admin"])
    access = login(auth, "admin@market.test").json()["access_token"]
    ok = inventory.get("/v1/warehouses", headers={"Authorization": f"Bearer {access}"})
    assert ok.status_code == 200
    add_user("cat@market.test", ["catalog_manager"])
    cat = login(auth, "cat@market.test").json()["access_token"]
    assert inventory.get("/v1/warehouses", headers={"Authorization": f"Bearer {cat}"}).status_code == 403


def test_forged_refresh_token_is_refused(auth, add_user):
    uid = add_user("lea@market.test", ["analyst"])
    forged = jwt.encode({"sub": str(uid), "typ": "refresh", "exp": 9999999999},
                        "another-key-that-is-long-enough-32-bytes!!", algorithm="HS256")
    assert auth.post("/v1/auth/refresh", json={"refresh_token": forged}).status_code == 401
