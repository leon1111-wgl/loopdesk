from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Tenant, User
from ..security import Principal, create_token, get_principal, rate_limiter, require_admin, verify_password
from ..seed import DEMO_PASSWORD, TENANTS

router = APIRouter(prefix="/api", tags=["auth"])


class LoginIn(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=128)


class TenantPatch(BaseModel):
    auto_refund_limit: float | None = Field(default=None, ge=0, le=100000)
    ticket_sla_hours: int | None = Field(default=None, ge=1, le=168)
    return_window_days: int | None = Field(default=None, ge=0, le=365)
    quality_window_days: int | None = Field(default=None, ge=0, le=365)


def user_out(user: User, tenant: Tenant) -> dict:
    return {
        "id": user.id, "email": user.email, "name": user.name, "role": user.role,
        "tenant": {"id": tenant.id, "slug": tenant.slug, "name": tenant.name, "industry": tenant.industry},
    }


def tenant_out(tenant: Tenant) -> dict:
    return {"id": tenant.id, "slug": tenant.slug, "name": tenant.name, "industry": tenant.industry,
            "auto_refund_limit": tenant.auto_refund_limit, "ticket_sla_hours": tenant.ticket_sla_hours,
            "return_window_days": tenant.return_window_days, "quality_window_days": tenant.quality_window_days}


@router.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    email = body.email.strip().lower()  # one key per account: " A@x" and "a@x" share the budget
    # Per client as well, or one attacker can try a few passwords on every account. This is
    # the TCP peer: behind a reverse proxy run uvicorn with --proxy-headers and
    # --forwarded-allow-ips=<proxy ip> instead of trusting X-Forwarded-For from anyone.
    client_ip = request.client.host if request.client else "unknown"
    rate_limiter.check(f"login-ip:{client_ip}", settings.login_attempts_per_ip)
    rate_limiter.check(f"login:{email}", settings.login_attempts_per_email)
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码错误")
    tenant = db.get(Tenant, user.tenant_id)
    return {"token": create_token(user), "user": user_out(user, tenant)}


@router.get("/auth/me")
def me(principal: Principal = Depends(get_principal)) -> dict:
    return user_out(principal.user, principal.tenant)


@router.get("/auth/demo-accounts")
def demo_accounts() -> dict:
    """Shown on the login page so reviewers can try every role. Disabled when
    LOOPDESK_SEED_DEMO=0 (i.e. in a real deployment)."""
    if not get_settings().seed_demo:
        return {"accounts": [], "password": None}
    accounts = [
        {"email": email, "name": name, "role": role, "tenant": spec["name"]}
        for spec in TENANTS for email, name, role in spec["users"]
    ]
    return {"accounts": accounts, "password": DEMO_PASSWORD}


@router.get("/tenant")
def get_tenant(principal: Principal = Depends(get_principal)) -> dict:
    return tenant_out(principal.tenant)


@router.patch("/tenant")
def patch_tenant(body: TenantPatch, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    tenant = db.get(Tenant, principal.tenant.id)
    if body.auto_refund_limit is not None:
        tenant.auto_refund_limit = body.auto_refund_limit
    if body.ticket_sla_hours is not None:
        tenant.ticket_sla_hours = body.ticket_sla_hours
    if body.return_window_days is not None:
        tenant.return_window_days = body.return_window_days
    if body.quality_window_days is not None:
        tenant.quality_window_days = body.quality_window_days
    db.commit()
    return tenant_out(tenant)


@router.delete("/admin/users/{user_id}/personal-data")
def erase_personal_data(user_id: int, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """Right to erasure (PIPL / GDPR style) for one user of the admin's tenant."""
    from ..ops.retention import erase_user
    from .common import get_owned

    target = get_owned(db, User, user_id, principal.tenant.id)
    if target.id == principal.user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能抹除自己的账号")
    return {"user_id": target.id, "erased": erase_user(db, target.id)}
