"""Authentication (JWT) and role-based access control."""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import RateCounter, Tenant, User
from .observability import identify

ROLES = ("admin", "agent", "customer")
STAFF_ROLES = ("admin", "agent")
_PBKDF2_ITERATIONS = 200_000
# Checked when a login names an account that does not exist, so that answer costs the same
# PBKDF2 work (iterations, salt length) as a wrong password. No password hashes to all zeros.
DUMMY_PASSWORD_HASH = f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${'00' * 16}${'00' * 32}"


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
        "ver": user.token_version,
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
    return principal_from_token(db, header.split(" ", 1)[1].strip())


def principal_from_token(db: Session, token: str) -> Principal:
    """The caller behind a bearer token; also used by the voice WebSocket, which receives the token
    in its first message (a browser cannot set headers on a WebSocket, and URLs end up in logs)."""
    claims = _decode(token)
    user = db.get(User, int(claims["sub"]))
    if user is None or user.tenant_id != claims.get("tid"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    if claims.get("ver", 0) != user.token_version:  # revoked: issued before an erasure
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效，请重新登录")
    tenant = db.get(Tenant, user.tenant_id)
    identify(tenant.id, user.id)
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
        self._lock = threading.Lock()  # sync endpoints run in a thread pool: check-and-record is one step

    def check(self, key: str, limit: int, window: float = 60.0) -> float | None:
        """Record one hit, or raise 429 if the window is full. Returns the hit, which
        release() can hand back."""
        if limit <= 0:
            return None
        with self._lock:
            now = time.monotonic()
            hits = self._hits[key]
            while hits and now - hits[0] > window:
                hits.popleft()
            if len(hits) >= limit:
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
            hits.append(now)
            return now

    def release(self, key: str, hit: float | None) -> None:
        if hit is None:
            return
        with self._lock:
            try:
                self._hits[key].remove(hit)
            except ValueError:  # already slid out of the window
                pass


rate_limiter = RateLimiter()


class DatabaseRateLimiter:
    """Fixed one-minute windows in the rate_counters table, shared by every worker process.

    One conditional upsert both checks and counts ("insert 1, or add 1 while below the
    limit"), so of two concurrent attempts at the limit exactly one gets the last slot. A
    fixed window can let up to twice the limit through across a minute boundary, which is
    acceptable for slowing down password guessing. Keys are stored hashed: the table would
    otherwise hold e-mail addresses and client IPs."""

    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self._swept = -1  # last minute this process deleted expired windows

    def check(self, db: Session, key: str, limit: int) -> tuple[str, int] | None:
        if limit <= 0:
            return None
        digest = hashlib.sha256(key.encode()).hexdigest()
        minute = int(self.clock() // 60)
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        allowed = db.execute(
            insert(RateCounter).values(key=digest, minute=minute, count=1)
            .on_conflict_do_update(index_elements=[RateCounter.key, RateCounter.minute],
                                   set_={"count": RateCounter.count + 1}, where=RateCounter.count < limit)
            .returning(RateCounter.count)
        ).first() is not None
        if self._swept != minute:  # at most once a minute per process, so the table stays small
            db.execute(delete(RateCounter).where(RateCounter.minute < minute - 1))
            self._swept = minute
        db.commit()
        if not allowed:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
        return digest, minute

    def release(self, db: Session, hit: tuple[str, int] | None) -> None:
        if hit is None:
            return
        digest, minute = hit
        db.execute(update(RateCounter).where(RateCounter.key == digest, RateCounter.minute == minute,
                                             RateCounter.count > 0).values(count=RateCounter.count - 1))
        db.commit()


shared_rate_limiter = DatabaseRateLimiter()
