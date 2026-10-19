"""Authentication (JWT) and role-based access control."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import Tenant, User
from .observability import tenant_var, user_var

ROLES = ("admin", "agent", "customer")
STAFF_ROLES = ("admin", "agent")
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, iterations, salt_hex, digest_hex = encoded.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    return hmac.compare_digest(digest.hex(), digest_hex)


def create_token(user: User) -> str:
    settings = get_settings()
    payload = {
        "sub": str(user.id),
        "tid": user.tenant_id,
        "role": user.role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def _decode(token: str) -> dict:
    try:
        return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效，请重新登录") from exc


class Principal:
    """The authenticated caller. Tenant and role come from the server-side user row,
    never from request bodies or model output."""

    def __init__(self, user: User, tenant: Tenant):
        self.user = user
        self.tenant = tenant

    @property
    def is_staff(self) -> bool:
        return self.user.role in STAFF_ROLES


def get_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少登录凭证")
    claims = _decode(header.split(" ", 1)[1].strip())
    user = db.get(User, int(claims["sub"]))
    if user is None or user.tenant_id != claims.get("tid"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    tenant = db.get(Tenant, user.tenant_id)
    tenant_var.set(tenant.id)
    user_var.set(user.id)
    return Principal(user, tenant)


def require_roles(*roles: str):
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "当前角色无权执行该操作")
        return principal

    return dependency


require_staff = require_roles(*STAFF_ROLES)
require_admin = require_roles("admin")


class RateLimiter:
    """Sliding-window limiter kept in process memory. Swap for Redis when running
    more than one replica."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window: float = 60.0) -> None:
        if limit <= 0:
            return
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
        hits.append(now)


rate_limiter = RateLimiter()
