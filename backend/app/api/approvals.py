from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..channels.feishu import FeishuClient
from ..db import get_db
from ..models import Approval, Conversation, Run, User
from ..security import Principal, require_staff
from ..services import get_services
from .common import get_owned, iso

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = Field(default="", max_length=500)


def approval_out(db: Session, a: Approval) -> dict:
    requester = db.get(User, a.requested_by)
    reviewer = db.get(User, a.reviewer_id) if a.reviewer_id else None
    run = db.get(Run, a.run_id)
    return {
        "id": a.id, "run_id": a.run_id, "conversation_id": a.conversation_id, "tool_name": a.tool_name,
        "arguments": a.arguments, "reason": a.reason, "evidence": a.evidence, "status": a.status,
        "requested_by": {"id": requester.id, "name": requester.name, "role": requester.role} if requester else None,
        "reviewer": {"id": reviewer.id, "name": reviewer.name} if reviewer else None,
        "question": run.input_text if run else "", "comment": a.comment,
        "created_at": iso(a.created_at), "decided_at": iso(a.decided_at),
    }


@router.get("")
def list_approvals(status_filter: str = "pending", principal: Principal = Depends(require_staff),
                   db: Session = Depends(get_db)) -> list[dict]:
    query = select(Approval).where(Approval.tenant_id == principal.tenant.id)
    if status_filter != "all":
        query = query.where(Approval.status == status_filter)
    rows = db.execute(query.order_by(Approval.id.desc()).limit(200)).scalars().all()
    return [approval_out(db, a) for a in rows]


@router.post("/{approval_id}/decide")
def decide(approval_id: int, body: DecisionIn, principal: Principal = Depends(require_staff),
           db: Session = Depends(get_db)) -> dict:
    approval = get_owned(db, Approval, approval_id, principal.tenant.id)
    if approval.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "该审批单已处理")
    if approval.requested_by == principal.user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "不能审批自己发起的申请（四眼原则）")
    services = get_services()
    events = services.runtime(db).resume(approval, body.decision, principal.user, body.comment)
    final = next((e for e in events if e["type"] == "final"), {})

    conversation = db.get(Conversation, approval.conversation_id) if approval.conversation_id else None
    if conversation and conversation.channel == "feishu" and conversation.external_id and final.get("text"):
        FeishuClient(services.settings).send_to_chat(conversation.external_id, final["text"])
    return {"approval": approval_out(db, approval), "final": final}


@router.get("/{approval_id}")
def get_approval(approval_id: int, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    return approval_out(db, get_owned(db, Approval, approval_id, principal.tenant.id))
