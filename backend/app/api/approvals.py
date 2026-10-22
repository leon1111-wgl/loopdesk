from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import audit
from ..admission import get_admission
from ..agent.business_tools import WITHDRAWN_COMMENT, end_paused_run, reviewer_role
from ..agent.runtime import REQUESTER_WITHDREW, ResumeError, decided_text
from ..channels.feishu import FeishuClient
from ..db import get_db
from ..models import Approval, Conversation, Refund, Run, Tenant, User
from ..security import Principal, require_staff
from ..services import get_services
from .common import get_owned, iso, masked

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = Field(default="", max_length=500)


def required_role(a: Approval, tenant: Tenant) -> str:
    """Who may approve: above the tenant's threshold, an administrator only. The four-eyes rule
    alone (no self-approval) left any single agent able to release any amount, because the
    requester is usually the customer. A tenant-declared tool's policy names the role itself (in its
    own amounts and currency), and that is the one enforced."""
    if a.approver_role:
        return a.approver_role
    return reviewer_role(tenant, float((a.arguments or {}).get("amount") or 0))


def approval_out(db: Session, a: Approval, refunds: dict[int, Refund] | None = None) -> dict:
    requester = db.get(User, a.requested_by)
    reviewer = db.get(User, a.reviewer_id) if a.reviewer_id else None
    run = db.get(Run, a.run_id)
    return {
        "id": a.id, "run_id": a.run_id, "conversation_id": a.conversation_id, "tool_name": a.tool_name,
        "arguments": a.arguments, "reason": a.reason, "evidence": a.evidence, "status": a.status,
        "requested_by": {"id": requester.id, "name": requester.name, "role": requester.role} if requester else None,
        "reviewer": {"id": reviewer.id, "name": reviewer.name} if reviewer else None,
        "question": run.input_text if run else "", "comment": a.comment,
        "required_role": required_role(a, db.get(Tenant, a.tenant_id)),
        # who called it off: the customer in the chat (N-127) or the member of staff who filed it (N-162)
        "withdrawn_by": ({WITHDRAWN_COMMENT: "customer", REQUESTER_WITHDREW: "requester"}.get(a.comment, "customer")
                         if a.status == "withdrawn" else None),
        "created_at": iso(a.created_at), "decided_at": iso(a.decided_at),
        "refund": _refund_out(db, a, refunds),
    }


def _refund_out(db: Session, a: Approval, refunds: dict[int, Refund] | None) -> dict | None:
    """Where the refund an approval filed stands (the ledger row): pending means the payment system
    has not answered yet, and a ticket is opened if nobody can settle it. ``refunds``: the ledger rows
    of a whole page of approvals, read at once."""
    if refunds is not None:
        refund = refunds.get(a.id)
    else:
        refund = db.execute(select(Refund).where(Refund.approval_id == a.id)).scalar_one_or_none()
    if refund is None:
        return None
    return {"id": refund.id, "order_id": refund.order_id, "amount": refund.amount, "status": refund.status,
            "external_id": refund.external_id, "attempts": refund.attempts, "ticket_id": refund.ticket_id,
            "updated_at": iso(refund.updated_at)}


@router.get("")
def list_approvals(status_filter: str = "pending", principal: Principal = Depends(require_staff),
                   db: Session = Depends(get_db)) -> list[dict]:
    query = select(Approval).where(Approval.tenant_id == principal.tenant.id)
    if status_filter != "all":
        query = query.where(Approval.status == status_filter)
    rows = db.execute(query.order_by(Approval.id.desc()).limit(200)).scalars().all()
    refunds = {r.approval_id: r for r in db.execute(select(Refund).where(
        Refund.approval_id.in_([a.id for a in rows]))).scalars()} if rows else {}
    return [approval_out(db, a, refunds) for a in rows]


@router.post("/{approval_id}/decide")
def decide(approval_id: int, body: DecisionIn, principal: Principal = Depends(require_staff),
           db: Session = Depends(get_db)) -> dict:
    approval = get_owned(db, Approval, approval_id, principal.tenant.id)
    if approval.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, decided_text(approval.status, approval.comment))
    if approval.requested_by == principal.user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "不能审批自己发起的申请（四眼原则）")
    if body.decision == "reject" and not body.comment.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "驳回必须填写原因：顾客会看到这条意见")
    if (body.decision == "approve" and required_role(approval, principal.tenant) == "admin"
            and principal.user.role != "admin"):
        threshold = principal.tenant.approval_admin_threshold
        if approval.approver_role:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "按这个工具的审批策略，需要管理员审批（坐席可以驳回）")
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"金额超过 ¥{threshold:.0f}，需要管理员审批（坐席可以驳回）")
    services = get_services()
    # Resuming runs the agent (and may call the model): it takes a slot like a chat message (N-189).
    # A process slot only: a decision unblocks a customer, so a tenant busy with its own customers
    # (at its share) can still decide.
    admission = get_admission()
    slot = admission.enter(None, None)
    try:
        events = services.runtime(db).resume(approval, body.decision, principal.user, masked(body.comment))
    except ResumeError as exc:  # the approval is pending again and can be retried
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except ValueError as exc:  # another reviewer decided first
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    finally:
        admission.leave(slot)
    final = next((e for e in events if e["type"] == "final"), {})

    # The decision has been executed and committed (a refund may have been paid). Telling the
    # customer on Feishu is best effort: if it fails, the reviewer must not see a 500 and retry
    # a decision that already took effect; the answer is in the conversation either way.
    delivery = None
    conversation = db.get(Conversation, approval.conversation_id) if approval.conversation_id else None
    if conversation and conversation.channel == "feishu" and conversation.external_id and final.get("text"):
        try:
            delivery = FeishuClient(services.settings, principal.tenant.slug).send_to_chat(
                conversation.external_id, final["text"])
        except Exception:
            logger.exception("feishu delivery of approval %s failed", approval.id)
            delivery = {"sent": False, "error": "飞书通知发送失败，结果已写入对话"}
    return {"approval": approval_out(db, approval), "final": final, "delivery": delivery}


@router.post("/{approval_id}/withdraw")
def withdraw(approval_id: int, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    """The member of staff who filed an application takes it back (N-162): a wrong amount, or nobody else
    who could approve it. Withdrawing decides nothing and moves no money, so the four-eyes rule does not
    stand in the way; nothing is sent to the customer (a rejection's comment would be). Customers withdraw
    their own in the chat, with a confirmation (N-145)."""
    approval = get_owned(db, Approval, approval_id, principal.tenant.id)
    if approval.requested_by != principal.user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有提交人可以撤回这笔申请")
    if approval.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, decided_text(approval.status, approval.comment))
    now = datetime.now(timezone.utc)
    # Conditional, like a reviewer's claim: one decided a moment ago stays decided.
    if db.execute(update(Approval).where(Approval.id == approval.id, Approval.status == "pending")
                  .values(status="withdrawn", reviewer_id=None, comment=REQUESTER_WITHDREW, decided_at=now),
                  execution_options={"synchronize_session": False}).rowcount != 1:
        db.rollback()
        db.refresh(approval)
        raise HTTPException(status.HTTP_409_CONFLICT, decided_text(approval.status, approval.comment))
    args = approval.arguments or {}
    amount = float(args.get("amount") or 0)
    end_paused_run(db, approval, now, f"{principal.user.name} 已撤回订单 {args.get('order_id')} 的退款申请"
                                      f"（¥{amount:.2f}），没有执行，也没有通知顾客。")
    audit.record(db, principal, "approval.withdraw", f"approval:{approval.id}", tool=approval.tool_name,
                 order_id=args.get("order_id"), amount=args.get("amount"), comment=REQUESTER_WITHDREW)
    db.commit()
    db.refresh(approval)
    return {"approval": approval_out(db, approval)}


@router.get("/{approval_id}")
def get_approval(approval_id: int, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    return approval_out(db, get_owned(db, Approval, approval_id, principal.tenant.id))
