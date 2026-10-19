from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..db import Base


def as_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo on read; every timestamp in this app is written in UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value else None


def get_owned(db: Session, model: type[Base], obj_id: Any, tenant_id: int):
    """Fetch a row and enforce tenant isolation. A row from another tenant is reported
    as 404, not 403, so IDs cannot be probed across tenants."""
    obj = db.get(model, obj_id)
    if obj is None or getattr(obj, "tenant_id", None) != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资源不存在")
    return obj
