from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import ACTIONS
from ..db import get_db
from ..models import AuditLog, User
from ..security import Principal, require_admin
from .common import iso

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
def list_audit(action: str | None = None, limit: int = Query(default=100, ge=1, le=500),
               principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    query = select(AuditLog).where(AuditLog.tenant_id == principal.tenant.id)
    if action:
        query = query.where(AuditLog.action == action)
    rows = db.execute(query.order_by(AuditLog.id.desc()).limit(limit)).scalars().all()
    actors = {u.id: u for u in db.execute(
        select(User).where(User.id.in_({r.actor_id for r in rows if r.actor_id}))).scalars()}
    return [
        {"id": r.id, "action": r.action, "action_label": ACTIONS.get(r.action, r.action), "target": r.target,
         "detail": r.detail, "request_id": r.request_id, "created_at": iso(r.created_at),
         "actor": ({"id": r.actor_id, "name": actors[r.actor_id].name, "email": actors[r.actor_id].email}
                   if r.actor_id in actors else None)}
        for r in rows
    ]
