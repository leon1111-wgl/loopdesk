from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..db import Base
from ..localtime import as_utc  # noqa: F401 (re-exported: most API modules import it from here)


def iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value else None


def get_owned(db: Session, model: type[Base], obj_id: Any, tenant_id: int):
    """Fetch a row and enforce tenant isolation. A row from another tenant is reported
    as 404, not 403, so IDs cannot be probed across tenants."""
    obj = db.get(model, obj_id)
    if obj is None or getattr(obj, "tenant_id", None) != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资源不存在")
    return obj
