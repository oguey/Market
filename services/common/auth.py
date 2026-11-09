"""JWT authentication + role-based authorization shared by every service.

The API gateway already checks the token, but each service checks it again
(defense in depth) and is the only place where *roles* are enforced.
"""
import logging
import os
import threading
import time

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

log = logging.getLogger("auth")

ALGORITHM = "HS256"
ROLES = {"admin", "catalog_manager", "analyst", "logistics", "service_account"}

_bearer = HTTPBearer(auto_error=False)


def _secret() -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET is not set")
    return secret


def create_token(sub: str, roles: list[str], ttl_seconds: int = 900) -> str:
    now = int(time.time())
    claims = {"sub": sub, "roles": roles, "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(claims, _secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, _secret(), algorithms=[ALGORITHM], options={"require": ["sub", "exp"]})


def _deny(status: int, detail: str, request: Request, user: str = "-") -> HTTPException:
    log.warning("access denied status=%s user=%s action=%s %s reason=%s",
                status, user, request.method, request.url.path, detail)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return HTTPException(status, detail, headers=headers)


def current_user(request: Request,
                 creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    if creds is None:
        raise _deny(401, "missing bearer token", request)
    try:
        return decode_token(creds.credentials)
    except jwt.ExpiredSignatureError:
        raise _deny(401, "token expired", request)
    except jwt.InvalidTokenError:
        raise _deny(401, "invalid token", request)


def has_role(user: dict, roles: tuple[str, ...]) -> bool:
    return bool(set(user.get("roles", [])) & set(roles))


def require_roles(*roles: str):
    """FastAPI dependency: `Depends(require_roles("admin", "analyst"))`. Deny by default."""
    unknown = set(roles) - ROLES
    if unknown:
        raise ValueError(f"unknown roles: {unknown}")

    def dependency(request: Request, user: dict = Depends(current_user)) -> dict:
        if not has_role(user, roles):
            raise _deny(403, "insufficient role", request, user.get("sub", "-"))
        return user

    return dependency


class ServiceToken:
    """Short-lived `service_account` token used for service-to-service calls, cached until near expiry."""

    def __init__(self, sub: str, ttl_seconds: int = 300):
        self.sub, self.ttl = sub, ttl_seconds
        self._token, self._exp = "", 0.0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            if time.time() > self._exp - 30:
                self._token = create_token(self.sub, ["service_account"], self.ttl)
                self._exp = time.time() + self.ttl
            return self._token
