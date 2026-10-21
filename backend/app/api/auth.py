from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..audit import changes, record
from ..db import get_db
from ..guardrails.injection import clean_domain
from ..localtime import is_valid_zone
from ..models import Tenant, User
from ..security import (DUMMY_PASSWORD_HASH, Principal, create_token, get_principal, rate_limiter, require_admin,
                        require_staff, shared_rate_limiter, verify_password)
from ..seed import DEMO_PASSWORD, FEATURED_TENANT, TENANTS

router = APIRouter(prefix="/api", tags=["auth"])


class LoginIn(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=128)


class TenantPatch(BaseModel):
    auto_refund_limit: float | None = Field(default=None, ge=0, le=100000)
    ticket_sla_hours: int | None = Field(default=None, ge=1, le=168)
    return_window_days: int | None = Field(default=None, ge=0, le=365)
    quality_window_days: int | None = Field(default=None, ge=0, le=365)
    approval_admin_threshold: float | None = Field(default=None, ge=0, le=1000000)
    timezone: str | None = Field(default=None, max_length=64)
    # Official channels (whole lists): sites answers may link to, accounts that are the tenant's.
    official_domains: list[str] | None = Field(default=None, max_length=50)
    official_contacts: list[str] | None = Field(default=None, max_length=50)


logger = logging.getLogger(__name__)
CONTACT_RE = re.compile(r"^(?:[a-z][a-z0-9_-]{4,63}|\d{5,15})$")


def user_out(user: User, tenant: Tenant) -> dict:
    return {
        "id": user.id, "email": user.email, "name": user.name, "role": user.role,
        "tenant": {"id": tenant.id, "slug": tenant.slug, "name": tenant.name, "industry": tenant.industry},
    }


def tenant_out(tenant: Tenant) -> dict:
    return {"id": tenant.id, "slug": tenant.slug, "name": tenant.name, "industry": tenant.industry,
            "auto_refund_limit": tenant.auto_refund_limit, "ticket_sla_hours": tenant.ticket_sla_hours,
            "return_window_days": tenant.return_window_days, "quality_window_days": tenant.quality_window_days,
            "approval_admin_threshold": tenant.approval_admin_threshold, "timezone": tenant.timezone,
            "official_domains": list(tenant.official_domains or []),
            "official_contacts": list(tenant.official_contacts or [])}


def _login_limiter(backend: str, db: Session):
    """(check, release) for login attempts. With several worker processes an in-memory
    limiter multiplies the limit by the number of workers, so PostgreSQL deployments (the
    multi-worker setup) count in the database by default."""
    if backend == "database" or (backend == "auto" and db.get_bind().dialect.name == "postgresql"):
        return (lambda key, limit: shared_rate_limiter.check(db, key, limit),
                lambda key, hit: shared_rate_limiter.release(db, hit))
    return rate_limiter.check, rate_limiter.release


@router.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    email = body.email.strip().lower()  # one key per account: " A@x" and "a@x" share the budget
    # Per client as well, or one attacker can try a few passwords on every account. This is
    # the TCP peer: behind a reverse proxy run uvicorn with --proxy-headers and
    # --forwarded-allow-ips=<proxy ip> instead of trusting X-Forwarded-For from anyone.
    client_ip = request.client.host if request.client else "unknown"
    ip_key, email_key = f"login-ip:{client_ip}", f"login:{email}"
    check, release = _login_limiter(settings.rate_limit_backend, db)
    ip_hit = check(ip_key, settings.login_attempts_per_ip)
    email_hit = check(email_key, settings.login_attempts_per_email)
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    # An unknown e-mail is checked against a stand-in hash: refused before PBKDF2 (tens of
    # ms), it would answer faster than a wrong password and timing would list the accounts.
    password_ok = verify_password(body.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if user is None or not password_ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码错误")
    # Only failures use up the budget, so an office behind one NAT or a user on several
    # devices is not locked out. Every attempt still holds its slot while the (slow)
    # password check runs, so a burst of parallel guesses cannot slip past the limit.
    release(ip_key, ip_hit)
    release(email_key, email_hit)
    tenant = db.get(Tenant, user.tenant_id)
    return {"token": create_token(user), "user": user_out(user, tenant)}


def _revoke_tokens(db: Session, user_id: int) -> None:
    """Every token issued to the user so far stops working on its next request. One counter per
    user, so signing out ends every device's session; a per-session list would need a table of
    issued tokens. The increment runs in SQL, so two concurrent sign-outs both count."""
    db.execute(update(User).where(User.id == user_id).values(token_version=User.token_version + 1)
               .execution_options(synchronize_session=False))


@router.post("/auth/logout")
def logout(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    """Server-side sign-out (N-043). Deleting the token in the browser is not enough: a copy
    taken from localStorage, a proxy log or a shared machine kept working until it expired."""
    _revoke_tokens(db, principal.user.id)
    db.commit()
    return {"signed_out": True}


@router.get("/auth/me")
def me(principal: Principal = Depends(get_principal)) -> dict:
    return user_out(principal.user, principal.tenant)


@router.get("/auth/demo-accounts")
def demo_accounts() -> dict:
    """Shown on the login page so reviewers can try every role. Disabled when
    LOOPDESK_SEED_DEMO=0 (i.e. in a real deployment)."""
    if not get_settings().seed_demo:
        return {"accounts": [], "password": None}
    featured_first = sorted(TENANTS, key=lambda spec: spec["slug"] != FEATURED_TENANT)
    accounts = [
        {"email": email, "name": name, "role": role, "tenant": spec["name"]}
        for spec in featured_first for email, name, role in spec["users"]
    ]
    return {"accounts": accounts, "password": DEMO_PASSWORD}


@router.get("/tenant")
def get_tenant(principal: Principal = Depends(require_staff)) -> dict:
    """Staff only: the settings include risk controls (the automatic refund limit)."""
    return tenant_out(principal.tenant)


@router.patch("/tenant")
def patch_tenant(body: TenantPatch, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    tenant = db.get(Tenant, principal.tenant.id)
    before = tenant_out(tenant)
    if body.auto_refund_limit is not None:
        tenant.auto_refund_limit = body.auto_refund_limit
    if body.ticket_sla_hours is not None:
        tenant.ticket_sla_hours = body.ticket_sla_hours
    if body.return_window_days is not None:
        tenant.return_window_days = body.return_window_days
    if body.quality_window_days is not None:
        tenant.quality_window_days = body.quality_window_days
    if body.approval_admin_threshold is not None:
        tenant.approval_admin_threshold = body.approval_admin_threshold
    if body.timezone is not None:
        if not is_valid_zone(body.timezone):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知的时区：{body.timezone}（请用 IANA 名称，例如 Asia/Shanghai）")
        tenant.timezone = body.timezone
    if body.official_domains is not None:
        domains = []
        for value in body.official_domains:
            domain = clean_domain(value)
            if not domain:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不是有效的域名：{value}（例如 leonshop.example，子域名自动包含）")
            domains.append(domain)
        tenant.official_domains = list(dict.fromkeys(domains))
    if body.official_contacts is not None:
        contacts = []
        for value in body.official_contacts:
            account = value.strip().lower()
            if not CONTACT_RE.match(account):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不是有效的账号：{value}（微信号、QQ 号或群号）")
            contacts.append(account)
        tenant.official_contacts = list(dict.fromkeys(contacts))
    after = tenant_out(tenant)
    diff = changes(before, after)
    if diff:  # risk limits and after-sales windows decide whether money moves without a person
        record(db, principal, "tenant.update", f"tenant:{tenant.id}", changes=diff)
    db.commit()
    if any(key in diff for key in ("official_domains", "official_contacts")):
        # What is outside the official channels changed: the knowledge is scanned again with them.
        # The change is saved already; a failed re-scan is reported, and every start re-scans anyway.
        from ..ops.rescan_knowledge import rescan
        try:
            after["rescan"] = rescan(db, tenant.id)
        except Exception:
            logger.exception("re-scanning tenant %s after a channel change failed", tenant.id)
            db.rollback()
            after["rescan"] = {"error": "已保存，但重新扫描知识库失败；服务下次启动时会再扫描一次"}
    return after


@router.delete("/admin/users/{user_id}/personal-data")
def erase_personal_data(user_id: int, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """Right to erasure (PIPL / GDPR style) for one user of the admin's tenant."""
    from ..ops.retention import RunInFlight, erase_user
    from .common import get_owned

    target = get_owned(db, User, user_id, principal.tenant.id)
    if target.id == principal.user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能抹除自己的账号")
    try:
        erased = erase_user(db, target.id, principal)
    except RunInFlight as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    # The entry names the account by id only: its e-mail and name are what was just erased.
    record(db, principal, "user.erase", f"user:{target.id}", erased=erased)
    db.commit()
    return {"user_id": target.id, "erased": erased}


@router.post("/admin/users/{user_id}/sign-out")
def force_sign_out(user_id: int, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """Sign a user of the admin's tenant out of every device, e.g. a lost laptop or a leaked
    token. The account itself keeps working: the user signs in again with the password."""
    from .common import get_owned

    target = get_owned(db, User, user_id, principal.tenant.id)
    _revoke_tokens(db, target.id)
    record(db, principal, "user.sign_out", f"user:{target.id}")  # by id, like user.erase
    db.commit()
    return {"user_id": target.id, "signed_out": True}
