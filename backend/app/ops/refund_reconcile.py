"""Settle refunds the payment system has not answered for, and follow the ones it is still making.

    python -m app.ops.refund_reconcile      # every few minutes, e.g. from cron or a Kubernetes CronJob

A refund is written to the ledger (pending) before the payment system is asked, and stays pending
when no answer comes back: a timeout, a dropped connection, a 5xx after the retries, a reply that
breaks the contract, a 409 that is not a refusal, or a process that died in between. It may have
been made. Its order stays held, so no second refund can be filed for it, and this job asks again
with the same request under the same idempotency key until an answer comes: the refund already
made, or one made now (once), or a refusal, which frees the order. Refunds the payment system
accepted but has not finished (processing, in a merchant's own system) are followed with a status
query until they succeed or fail.

Each pending refund is leased with a conditional UPDATE before it is sent, so overlapping runs (a
CronJob allows them) do not send it twice at once, and one row that fails (a bug, a database error)
is logged and counted without stopping the others. One nobody could settle for an hour, and one that
failed after it was accepted (the customer was told it would be paid), becomes a ticket in the staff
workbench; a ticket closed while the refund is still unsettled opens a new one. After 24 hours a
pending refund is no longer sent: payment systems keep idempotency keys for a limited time (the
contract asks for at least 7 days), and past it a re-send is a new request. A person checks the
payment system's back office and records the outcome (POST /api/refunds/{id}/settle). Neither outcome
is sent to the customer by this job.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..agent.business_tools import file_refund
from ..config import Settings
from ..db import new_session
from ..integrations import Commerce, GatewayError
from ..models import Approval, Refund, Tenant, Ticket

# A refund younger than this may still be in its first filing (one call takes at most
# CommerceApi.call_seconds, under a minute); a run that leased a row holds it this long.
PENDING_AFTER = timedelta(minutes=2)
ESCALATE_AFTER = timedelta(hours=1)  # unsettled this long: a person looks at it
RESEND_WINDOW = timedelta(hours=24)  # sent again only this long after it was filed; well inside key retention
FOLLOW_DAYS = 30  # processing refunds older than this are left to the merchant's own reconciliation
BATCH = 200

logger = logging.getLogger(__name__)


def reconcile_refunds(settings: Settings, commerce: Commerce | None = None, older_than: timedelta = PENDING_AFTER,
                      now: datetime | None = None) -> dict[str, int]:
    """``now`` (tests) only decides what counts as old; rows record the real time."""
    commerce = commerce or Commerce(settings)
    now = now or datetime.now(timezone.utc)
    cutoff = now - older_than
    counts = {"confirmed": 0, "refused": 0, "still_pending": 0, "escalated": 0, "succeeded": 0, "failed": 0,
              "processing": 0, "stuck": 0, "errored": 0}
    with new_session() as db:
        pending = db.execute(
            select(Refund.id).where(Refund.status == "pending",
                                    func.coalesce(Refund.updated_at, Refund.created_at) <= cutoff)
            .order_by(Refund.id).limit(BATCH)
        ).scalars().all()
        remote = [t.id for t in db.execute(select(Tenant)).scalars() if commerce.via(t) == "http"]
        processing = db.execute(
            select(Refund.id).where(Refund.status == "processing", Refund.tenant_id.in_(remote),
                                    Refund.external_id.is_not(None),
                                    Refund.created_at >= now - timedelta(days=FOLLOW_DAYS))
            .order_by(Refund.id).limit(BATCH)
        ).scalars().all() if remote else []
        # "claiming" exists only inside the transaction that writes it; a committed one is a bug that
        # holds an order with nobody filing for it.
        stuck = db.execute(select(Refund.id).where(Refund.status == "claiming", Refund.created_at <= cutoff)
                           ).scalars().all()
    for refund_id in stuck:
        logger.error("refund %s is committed as 'claiming': it holds its order and was never filed", refund_id)
        counts["stuck"] += 1
    for refund_id in pending:
        try:
            outcome = _settle_pending(commerce, refund_id, cutoff, now, counts)
        except Exception:  # one row must not stop the rest (they are taken in id order, every run)
            logger.exception("reconciling refund %s failed", refund_id)
            outcome = "errored"
        if outcome is not None:
            counts[outcome] += 1
    for refund_id in processing:
        try:
            outcome = _follow(commerce, refund_id, counts)
        except Exception:
            logger.exception("following refund %s failed", refund_id)
            outcome = "errored"
        if outcome is not None:
            counts[outcome] += 1
    return counts


def _settle_pending(commerce: Commerce, refund_id: int, cutoff: datetime, now: datetime,
                    counts: dict[str, int]) -> str | None:
    with new_session() as db:
        if not _lease(db, refund_id, cutoff):
            return None  # settled meanwhile, or another run is filing it
        refund = db.get(Refund, refund_id)
        if _as_utc(refund.created_at) > now - RESEND_WINDOW:
            file_refund(db, commerce.refunds(db.get(Tenant, refund.tenant_id), db), refund)
        if refund.status == "pending" and _as_utc(refund.created_at) <= now - ESCALATE_AFTER and _unattended(db, refund):
            resend = ("对账任务仍会用原来的幂等键重发" if _as_utc(refund.created_at) > now - RESEND_WINDOW
                      else "已超过 24 小时，不再自动重发（对方可能已不保留这个幂等键，再发可能重复退款）")
            _escalate(db, refund, f"支付系统一直没有确认这笔退款（已发送 {refund.attempts} 次，最近一次：{refund.error}），"
                                  f"订单仍被占用，{resend}。请在支付系统后台核实是否已退款，再用 "
                                  f"POST /api/refunds/{refund.id}/settle 记下结果（succeeded 或 failed）")
            counts["escalated"] += 1
        db.commit()
        if refund.status == "pending":
            logger.warning("refund %s (order %s) still has no answer from the payment system: %s",
                           refund.id, refund.order_id, refund.error)
            return "still_pending"
        return "refused" if refund.status == "failed" else "confirmed"


def _follow(commerce: Commerce, refund_id: int, counts: dict[str, int]) -> str | None:
    with new_session() as db:
        refund = db.get(Refund, refund_id)
        if refund is None or refund.status != "processing":
            return None
        try:
            result = commerce.refunds(db.get(Tenant, refund.tenant_id), db).get_refund(refund.external_id)
        except GatewayError as exc:
            logger.warning("refund %s: status query failed: %s", refund.id, exc.detail)
            return "processing"
        if result.status != refund.status:
            refund.status, refund.updated_at = result.status, datetime.now(timezone.utc)
            refund.error = "failed" if result.status == "failed" else ""
            if result.status == "failed" and _unattended(db, refund):
                _escalate(db, refund, "支付系统受理后又报告退款失败，顾客此前被告知会按原支付方式退回。"
                                      "请联系顾客并在支付系统里处理")
                counts["escalated"] += 1
                logger.error("refund %s (order %s) failed after the payment system accepted it",
                             refund.id, refund.order_id)
            db.commit()
        return result.status


def _unattended(db: Session, refund: Refund) -> bool:
    """No ticket for it yet, or its ticket was closed while the refund is still unsettled."""
    ticket = db.get(Ticket, refund.ticket_id) if refund.ticket_id is not None else None
    return ticket is None or ticket.status == "closed"


def _lease(db: Session, refund_id: int, cutoff: datetime) -> bool:
    """One conditional UPDATE: of two runs that picked the same refund, one sends it; the other finds
    it touched after the cutoff and leaves it."""
    leased = db.execute(
        update(Refund).where(Refund.id == refund_id, Refund.status == "pending",
                             func.coalesce(Refund.updated_at, Refund.created_at) <= cutoff)
        .values(updated_at=datetime.now(timezone.utc)).execution_options(synchronize_session=False)
    ).rowcount == 1
    db.commit()
    return leased


def _escalate(db: Session, refund: Refund, why: str) -> None:
    """A ticket in the staff workbench, linked from the refund (one open ticket per refund). It is in
    the conversation the refund was asked in: the resolution a person writes is sent there."""
    approval = db.get(Approval, refund.approval_id) if refund.approval_id else None
    ticket = Ticket(
        tenant_id=refund.tenant_id, conversation_id=refund.conversation_id or (approval.conversation_id if approval else None),
        customer_id=refund.customer_id, kind="issue", priority="urgent",
        title=f"退款 #{refund.id}（订单 {refund.order_id}，¥{refund.amount:.2f}）需要人工核实",
        description=why, due_at=datetime.now(timezone.utc) + timedelta(hours=4),
    )
    db.add(ticket)
    db.flush()
    refund.ticket_id = ticket.id


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def main() -> int:  # pragma: no cover - CLI wrapper
    from ..config import get_settings
    from ..db import init_engine

    settings = get_settings()
    init_engine(settings.database_url)
    print(json.dumps(reconcile_refunds(settings)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
