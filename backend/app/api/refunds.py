"""Refunds the payment system never settled: staff see them, an administrator records what its back
office shows.

A refund stays pending while the payment system has not answered (app/ops/refund_reconcile.py sends
it again for 24 hours, and opens a ticket after one). When it cannot settle it, a person checks the
payment system and records the outcome here: succeeded (the order keeps its one online refund) or
failed (nothing was paid; the order is free again). Audited, and only once. The list says, per row,
what the reconciliation job will still do about it, so the console shows which ones wait for a person.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session

from ..audit import record
from ..db import get_db
from ..models import Refund, User
from ..ops.refund_reconcile import FOLLOW_DAYS, RESEND_WINDOW
from ..security import Principal, require_admin, require_staff
from ..services import get_services
from .common import as_utc, get_owned, iso, masked

router = APIRouter(prefix="/api/refunds", tags=["refunds"])
LIMIT = 200
Filter = Literal["open", "pending", "processing", "succeeded", "failed", "all"]


def follow_up(refund: Refund, via: str, now: datetime) -> str | None:
    """What happens to an unsettled refund next, by the reconciliation job's own rules: "resend" (it
    is sent again under its key), "follow" (the payment system accepted it; its status is queried),
    "person" (the job no longer does anything: someone checks the payment system and settles it),
    "stuck" (a committed "claiming" row: a bug that holds its order; the job logs it), or "demo" (the
    demo tables move no money, so their refunds stay processing and nothing will answer)."""
    created = as_utc(refund.created_at)
    if refund.status == "claiming":
        return "stuck"
    if refund.status == "pending":
        return "resend" if created > now - RESEND_WINDOW else "person"
    if refund.status == "processing":
        if via == "local":
            return "demo"
        followed = refund.external_id is not None and created >= now - timedelta(days=FOLLOW_DAYS)
        return "follow" if followed else "person"
    return None


def _unsettled(via: str):
    return Refund.status.in_(("claiming", "pending", "processing") if via == "http" else ("claiming", "pending"))


def _needs_person(via: str, now: datetime):
    """follow_up() in SQL: the rows it calls "person" or "stuck"."""
    cond = or_(Refund.status == "claiming",
               and_(Refund.status == "pending", Refund.created_at <= now - RESEND_WINDOW))
    if via == "http":
        cond = or_(cond, and_(Refund.status == "processing",
                              or_(Refund.external_id.is_(None), Refund.created_at < now - timedelta(days=FOLLOW_DAYS))))
    return cond


@router.get("")
def list_refunds(status_filter: Filter = Query("open", alias="status"), principal: Principal = Depends(require_staff),
                 db: Session = Depends(get_db)) -> list[dict]:
    """The tenant's refund ledger, newest first (at most 200). ``open`` (the default) is what the
    payment system has not settled: pending everywhere, and processing where a payment system is
    still to report the end (not the demo tables, whose refunds stay processing for good). It lists
    the ones waiting for a person first: they are the oldest, and newer ones must not push them off."""
    via = get_services().commerce.via(principal.tenant)
    now = datetime.now(timezone.utc)
    query = (select(Refund, User).outerjoin(User, User.id == Refund.customer_id)
             .where(Refund.tenant_id == principal.tenant.id))
    order = [Refund.created_at.desc(), Refund.id.desc()]
    if status_filter == "open":
        query = query.where(_unsettled(via))
        order.insert(0, case((_needs_person(via, now), 0), else_=1))
    elif status_filter != "all":
        query = query.where(Refund.status == status_filter)
    rows = db.execute(query.order_by(*order).limit(LIMIT)).all()
    return [{
        "id": refund.id, "order_id": refund.order_id, "amount": refund.amount, "status": refund.status,
        "attempts": refund.attempts, "error": refund.error, "external_id": refund.external_id,
        "idempotency_key": refund.idempotency_key,  # what the payment system's back office finds a pending one by
        "ticket_id": refund.ticket_id, "approval_id": refund.approval_id, "conversation_id": refund.conversation_id,
        "customer": {"id": customer.id, "name": customer.name} if customer else None,
        "created_at": iso(refund.created_at), "updated_at": iso(refund.updated_at),
        "follow_up": follow_up(refund, via, now),
        "resend_until": iso(as_utc(refund.created_at) + RESEND_WINDOW) if refund.status == "pending" else None,
    } for refund, customer in rows]


@router.get("/summary")
def summary(principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    """Two counts for the navigation: unsettled refunds, and those waiting for a person. Polled by
    every staff page, so two COUNTs on (tenant_id, status, created_at), not the list."""
    via = get_services().commerce.via(principal.tenant)
    now = datetime.now(timezone.utc)
    mine = select(func.count()).select_from(Refund).where(Refund.tenant_id == principal.tenant.id, _unsettled(via))
    return {"unsettled": db.execute(mine).scalar_one(),
            "needs_person": db.execute(mine.where(_needs_person(via, now))).scalar_one()}


NOT_FAILED_YET = {
    "resend": "对账任务还在用原来的幂等键重发这笔退款，之后的重发仍可能退款成功：现在记为失败，顾客再申请就可能退两次。"
              "支付后台查到已退款的可以记为已退款；记为失败请等提交 24 小时后不再重发时。",
    "follow": "支付系统已经受理这笔退款，对账任务在查询结果：现在记为失败，它之后仍可能退款成功，顾客再申请就可能退两次。"
              "请等支付系统给出结果，或在支付后台查到已退款后记为已退款。",
    "demo": "演示订单表的退款不经过支付系统，记为失败会让演示订单一直处于退款中。",
}


class SettleIn(BaseModel):
    outcome: Literal["succeeded", "failed"]
    external_id: str | None = Field(default=None, max_length=128)  # the payment system's refund id, if any
    note: str = Field(min_length=2, max_length=500)  # what the back office showed, for the audit log


@router.post("/{refund_id}/settle")
def settle(refund_id: int, body: SettleIn, principal: Principal = Depends(require_admin),
           db: Session = Depends(get_db)) -> dict:
    refund = get_owned(db, Refund, refund_id, principal.tenant.id)
    if refund.status == "claiming":
        raise HTTPException(status.HTTP_409_CONFLICT, "这笔退款登记未完成：它占着订单，但没有提交给支付系统，不能在这里确认。"
                                                      "请按对账任务的 ERROR 日志排查")
    if body.outcome == "failed" and refund.status in ("pending", "processing"):
        # "failed" frees the order for another refund. While the job still re-sends this one under its
        # key, or the payment system may still finish the one it accepted, the first can be paid too.
        step = follow_up(refund, get_services().commerce.via(principal.tenant), datetime.now(timezone.utc))
        if step != "person":
            raise HTTPException(status.HTTP_409_CONFLICT, NOT_FAILED_YET[step])
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
           outcome=body.outcome, external_id=body.external_id, note=masked(body.note))
    db.commit()
    db.refresh(refund)
    return {"id": refund.id, "order_id": refund.order_id, "amount": refund.amount, "status": refund.status,
            "external_id": refund.external_id, "updated_at": iso(refund.updated_at)}
