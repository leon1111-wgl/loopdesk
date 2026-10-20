"""Handoff queue for staff: see who is waiting, reply as a person, hand back to the agent."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..agent import handoff
from ..audit import record
from ..channels.feishu import FeishuClient
from ..db import get_db
from ..models import Conversation, Message, Ticket, User
from ..security import Principal, require_staff
from ..services import get_services
from .chat import message_out
from .common import as_utc, get_owned, iso

router = APIRouter(prefix="/api/handoffs", tags=["handoffs"])
logger = logging.getLogger(__name__)

HANDBACK_TEXT = "人工服务已结束，之后的问题由智能客服继续为您解答。"


class ReplyIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


def handoff_out(db: Session, ticket: Ticket) -> dict:
    conversation = db.get(Conversation, ticket.conversation_id) if ticket.conversation_id else None
    customer = db.get(User, ticket.customer_id)
    last = db.execute(
        select(Message).where(Message.conversation_id == ticket.conversation_id, Message.role == "user")
        .order_by(Message.id.desc()).limit(1)
    ).scalar_one_or_none() if conversation else None
    return {
        **handoff.ticket_state(db, ticket),
        "conversation_id": ticket.conversation_id, "channel": conversation.channel if conversation else None,
        "customer": customer.name if customer else None, "reason": ticket.description, "priority": ticket.priority,
        "created_at": iso(ticket.created_at),
        "waiting_seconds": int((datetime.now(timezone.utc) - as_utc(ticket.created_at)).total_seconds()),
        "last_message": last.content[:120] if last else "",
    }


def reply_times(db: Session, conversation_ids: set[int]) -> dict[int, list[datetime]]:
    """When people (role=agent) wrote in these conversations."""
    times: dict[int, list[datetime]] = {}
    if conversation_ids:
        for conversation_id, created in db.execute(
            select(Message.conversation_id, Message.created_at)
            .where(Message.conversation_id.in_(conversation_ids), Message.role == "agent")
        ).all():
            times.setdefault(conversation_id, []).append(as_utc(created))
    return times


def first_reply(ticket: Ticket, times: dict[int, list[datetime]]) -> datetime | None:
    """The first human reply while this handoff was open. A conversation can be handed over
    more than once: a handoff ended without a reply must not take the next one's reply."""
    opened = as_utc(ticket.created_at)
    closed = as_utc(ticket.closed_at) if ticket.closed_at else None
    return min((t for t in times.get(ticket.conversation_id, []) if t >= opened and (closed is None or t <= closed)),
               default=None)


def _active(db: Session, principal: Principal, ticket_id: int) -> tuple[Ticket, Conversation]:
    ticket = get_owned(db, Ticket, ticket_id, principal.tenant.id)
    if ticket.kind != "handoff" or ticket.conversation_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资源不存在")
    if ticket.status not in handoff.ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, "该转人工请求已经结束")
    return ticket, db.get(Conversation, ticket.conversation_id)


def _deliver(conversation: Conversation, text: str) -> None:
    """Feishu customers are not looking at our web page: send the reply into their chat."""
    if conversation.channel != "feishu" or not conversation.external_id:
        return
    try:
        FeishuClient(get_services().settings).send_to_chat(conversation.external_id, text)
    except Exception:  # the message is stored either way; staff can retry from the channel
        logger.exception("feishu delivery for conversation %s failed", conversation.id)


@router.get("")
def list_handoffs(principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> list[dict]:
    """Waiting and in-progress handoffs, oldest first (that is the order people should be served in)."""
    tickets = db.execute(
        select(Ticket).where(Ticket.tenant_id == principal.tenant.id, Ticket.kind == "handoff",
                             Ticket.status.in_(handoff.ACTIVE)).order_by(Ticket.id)
    ).scalars().all()
    return [handoff_out(db, t) for t in tickets]


def _transition(db: Session, ticket: Ticket, to: str, user_id: int, **values) -> None:
    """Change a live ticket in one conditional UPDATE. The check in _active is a read: a reply
    and a close arriving together could both pass it, and the reply would reopen a ticket that
    had just been closed. The first person to act owns the ticket (COALESCE keeps an owner)."""
    claimed = db.execute(
        update(Ticket).where(Ticket.id == ticket.id, Ticket.status.in_(handoff.ACTIVE))
        .values(status=to, assignee_id=func.coalesce(Ticket.assignee_id, user_id), **values)
        .execution_options(synchronize_session="fetch")
    ).rowcount
    if claimed != 1:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "该转人工请求已经结束")


@router.post("/{ticket_id}/reply")
def reply(ticket_id: int, body: ReplyIn, principal: Principal = Depends(require_staff),
          db: Session = Depends(get_db)) -> dict:
    ticket, conversation = _active(db, principal, ticket_id)
    _transition(db, ticket, "in_progress", principal.user.id)
    message = Message(tenant_id=ticket.tenant_id, conversation_id=conversation.id, role="agent",
                      content=body.content.strip(), meta={"agent": principal.user.name, "agent_id": principal.user.id})
    conversation.updated_at = datetime.now(timezone.utc)
    db.add(message)
    db.flush()
    record(db, principal, "handoff.reply", f"handoff:{ticket.id}", conversation_id=conversation.id,
           message_id=message.id)
    db.commit()
    _deliver(conversation, f"人工客服 {principal.user.name}：{message.content}")
    return {"message": message_out(message), "handoff": handoff_out(db, ticket)}


@router.post("/{ticket_id}/close")
def close(ticket_id: int, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    """End the human service and hand the conversation back to the agent."""
    ticket, conversation = _active(db, principal, ticket_id)
    _transition(db, ticket, "closed", principal.user.id, closed_at=datetime.now(timezone.utc))
    conversation.status = "bot"
    conversation.updated_at = datetime.now(timezone.utc)
    db.add(Message(tenant_id=ticket.tenant_id, conversation_id=conversation.id, role="system", content=HANDBACK_TEXT))
    record(db, principal, "handoff.close", f"handoff:{ticket.id}", conversation_id=conversation.id)
    db.commit()
    _deliver(conversation, HANDBACK_TEXT)
    return {"ticket_id": ticket.id, "status": ticket.status, "conversation_id": conversation.id}
