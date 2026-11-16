"""Auth service — login (email + password -> JWT), token refresh, and admin user creation.

Passwords are stored as bcrypt hashes in auth.users. The access token carries only
`sub` (the user id, never the email), `roles` and `exp`, as the brief requires.
"""
import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from psycopg import errors
from pydantic import BaseModel, Field, StringConstraints

from common import auth as common_auth
from common import observability
from common.auth import ROLES, create_token, current_user, require_roles
from common.db import dsn_for, make_pool, ping

log = logging.getLogger("auth")

ACCESS_TTL = 900                 # 15 minutes, as in the brief
REFRESH_TTL = 7 * 24 * 3600      # 7 days
BCRYPT_ROUNDS = 12
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+$"
# Trimmed and lower-cased before the pattern is checked. (EmailStr would reject the .test domains of the seed.)
Email = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=EMAIL_PATTERN, max_length=254)]


class LoginIn(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=72)  # bcrypt ignores bytes past 72


class RefreshIn(BaseModel):
    refresh_token: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    roles: list[str]


class AccessOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    roles: list[str]


class UserIn(BaseModel):
    email: Email
    password: str = Field(min_length=8, max_length=72)
    roles: list[str] = Field(min_length=1)


class UserOut(BaseModel):
    id: int
    email: str
    roles: list[str]


class Me(BaseModel):
    sub: str
    roles: list[str]


def _check_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:  # malformed hash in the database: refuse, never crash
        return False


def _refresh_token(user_id: int) -> str:
    now = int(time.time())
    claims = {"sub": str(user_id), "typ": "refresh", "iat": now, "exp": now + REFRESH_TTL}
    return jwt.encode(claims, common_auth._secret(), algorithm=common_auth.ALGORITHM)


def _roles_of(conn, user_id: int) -> list[str]:
    rows = conn.execute("""SELECT r.name FROM auth.user_roles ur JOIN auth.roles r ON r.id = ur.role_id
                           WHERE ur.user_id = %s ORDER BY r.name""", (user_id,)).fetchall()
    return [r["name"] for r in rows]


def access_user(user: dict = Depends(current_user)) -> dict:
    """Like current_user, but a refresh token is not accepted as an access token."""
    if user.get("typ") == "refresh":
        raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"})
    return user


def create_app(dsn: str | None = None) -> FastAPI:
    pool = make_pool(dsn or dsn_for("auth_svc", "AUTH_DB_PASSWORD"))
    # Checked when the email is unknown, so "unknown email" and "wrong password" take the same time.
    dummy_hash = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(BCRYPT_ROUNDS)).decode()

    @asynccontextmanager
    async def lifespan(_):
        pool.open()
        yield
        pool.close()

    app = FastAPI(title="Market - Auth", version="1.0.0", lifespan=lifespan)
    observability.setup(app, "auth", lambda: ping(pool))

    def refuse(request: Request, email: str, reason: str) -> HTTPException:
        log.warning("login failed email=%s reason=%s action=%s %s", email, reason, request.method, request.url.path)
        return HTTPException(401, "invalid credentials", headers={"WWW-Authenticate": "Bearer"})

    @app.post("/v1/auth/login", response_model=TokenOut, tags=["auth"])
    def login(body: LoginIn, request: Request):
        """Checks the password and returns a 15-minute access token plus a refresh token."""
        email = body.email
        with pool.connection() as conn:
            user = conn.execute("SELECT id, password_hash, is_active FROM auth.users WHERE email = %s",
                                (email,)).fetchone()
            ok = _check_password(body.password, user["password_hash"] if user else dummy_hash)
            if not user:
                raise refuse(request, email, "unknown email")
            if not ok:
                raise refuse(request, email, "wrong password")
            if not user["is_active"]:
                raise refuse(request, email, "account disabled")
            roles = _roles_of(conn, user["id"])
        if not roles:
            raise refuse(request, email, "no role")
        log.info("login ok user=%s roles=%s", user["id"], roles)
        return {"access_token": create_token(str(user["id"]), roles, ACCESS_TTL),
                "refresh_token": _refresh_token(user["id"]),
                "expires_in": ACCESS_TTL, "roles": roles}

    @app.post("/v1/auth/refresh", response_model=AccessOut, tags=["auth"])
    def refresh(body: RefreshIn, request: Request):
        """Trades a refresh token for a new access token. Roles are re-read, so a role change applies."""
        try:
            claims = common_auth.decode_token(body.refresh_token)
        except jwt.InvalidTokenError:
            raise refuse(request, "-", "bad refresh token")
        if claims.get("typ") != "refresh":
            raise refuse(request, "-", "not a refresh token")
        with pool.connection() as conn:
            user = conn.execute("SELECT id, is_active FROM auth.users WHERE id = %s",
                                (int(claims["sub"]),)).fetchone()
            roles = _roles_of(conn, user["id"]) if user else []
        if not user or not user["is_active"] or not roles:
            raise refuse(request, claims["sub"], "user no longer allowed")
        return {"access_token": create_token(str(user["id"]), roles, ACCESS_TTL),
                "expires_in": ACCESS_TTL, "roles": roles}

    @app.get("/v1/auth/me", response_model=Me, tags=["auth"])
    def me(user: dict = Depends(access_user)):
        return {"sub": user["sub"], "roles": user.get("roles", [])}

    @app.post("/v1/auth/users", response_model=UserOut, status_code=201, tags=["users"])
    def create_user(body: UserIn, _=Depends(require_roles("admin"))):
        """Admin only: creates a user and assigns roles."""
        unknown = set(body.roles) - ROLES
        if unknown:
            raise HTTPException(422, f"unknown roles: {sorted(unknown)}")
        email = body.email
        pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()
        try:
            with pool.connection() as conn:
                user_id = conn.execute(
                    "INSERT INTO auth.users (email, password_hash) VALUES (%s, %s) RETURNING id",
                    (email, pw_hash)).fetchone()["id"]
                conn.execute("""INSERT INTO auth.user_roles (user_id, role_id)
                                SELECT %s, id FROM auth.roles WHERE name = ANY(%s)""", (user_id, body.roles))
                roles = _roles_of(conn, user_id)
                if set(roles) != set(body.roles):  # a role is missing from auth.roles: undo everything
                    raise HTTPException(422, "some roles do not exist in auth.roles")
        except errors.UniqueViolation:
            raise HTTPException(409, "email already used")
        return {"id": user_id, "email": email, "roles": roles}

    return app
