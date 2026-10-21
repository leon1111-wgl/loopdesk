from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..db import Base
from ..guardrails.injection import Channels
from ..guardrails.pii import mask_pii
from ..textutil import normalize_width
from ..localtime import as_utc  # noqa: F401 (re-exported: most API modules import it from here)


def iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value else None


def masked(text: str, tenant=None) -> str:
    """Free text from a write endpoint, as it may be stored (N-083). Customer messages were masked
    in the agent runtime, but a feedback comment, a conversation title or a staff note went to the
    database as typed, and a customer clicking "down" is the one most likely to leave a phone number.

    Pass ``tenant`` for staff text sent to the customer: the tenant's own addresses (official
    domains) and numbers (official contacts) stay readable, anyone else's are masked. Full-width
    digits are folded first, as in the runtime; text with nothing to mask is kept exactly as typed."""
    from ..services import get_services  # services imports the agent, which imports this package

    if not get_services().settings.pii_masking:
        return text
    keep = None
    if tenant is not None:
        channels = Channels.of(tenant.official_domains, tenant.official_contacts)
        numbers = {re.sub(r"\D", "", c)[-11:] for c in channels.contacts} - {""}

        def keep(kind: str, value: str) -> bool:
            if kind == "email":
                return channels.official_host(value.rpartition("@")[2].lower())
            return kind == "phone" and re.sub(r"\D", "", value)[-11:] in numbers

    report = mask_pii(normalize_width(text), keep)
    return report.masked if report.found else text


def get_owned(db: Session, model: type[Base], obj_id: Any, tenant_id: int):
    """Fetch a row and enforce tenant isolation. A row from another tenant is reported
    as 404, not 403, so IDs cannot be probed across tenants."""
    obj = db.get(model, obj_id)
    if obj is None or getattr(obj, "tenant_id", None) != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资源不存在")
    return obj
