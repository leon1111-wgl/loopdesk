"""Refunds the payment system never settled: an administrator records what its back office shows.

A refund stays pending while the payment system has not answered (app/ops/refund_reconcile.py sends
it again for 24 hours, and opens a ticket after one). When it cannot settle it, a person checks the
payment system and records the outcome here: succeeded (the order keeps its one online refund) or
failed (nothing was paid; the order is free again). Audited, and only once.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..audit import record
from ..db import get_db
from ..models import Refund
from ..security import Principal, require_admin
from .common import get_owned, iso

router = APIRouter(prefix="/api/refunds", tags=["refunds"])


class SettleIn(BaseModel):
    outcome: Literal["succeeded", "failed"]
    external_id: str | None = Field(default=None, max_length=128)  # the payment system's refund id, if any
    note: str = Field(min_length=2, max_length=500)  # what the back office showed, for the audit log


@router.post("/{refund_id}/settle")
def settle(refund_id: int, body: SettleIn, principal: Principal = Depends(require_admin),
           db: Session = Depends(get_db)) -> dict:
    refund = get_owned(db, Refund, refund_id, principal.tenant.id)
    values = {"status": body.outcome, "error": "settled by hand" if body.outcome == "failed" else "",
              "updated_at": datetime.now(timezone.utc)}
    if body.external_id:
        values["external_id"] = body.external_id
    # One conditional UPDATE: a reconciliation that settles it at the same moment, or a second
    # administrator, finds it settled.
    settled = db.execute(
        update(Refund).where(Refund.id == refund.id, Refund.status.in_(("pending", "processing")))
        .values(**values).execution_options(synchronize_session=False)
    ).rowcount
    if settled != 1:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "这笔退款已经有结果，不能再次确认")
    record(db, principal, "refund.settle", f"refund:{refund.id}", order_id=refund.order_id, amount=refund.amount,
           outcome=body.outcome, external_id=body.external_id, note=body.note)
    db.commit()
    db.refresh(refund)
    return {"id": refund.id, "order_id": refund.order_id, "amount": refund.amount, "status": refund.status,
            "external_id": refund.external_id, "updated_at": iso(refund.updated_at)}
