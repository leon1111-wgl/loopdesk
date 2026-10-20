"""Issue tickets the agent opens for customers ("技术支持将在 4 小时内联系您"): the staff side
that keeps that promise. Review round 3, N-019: the tickets were written and never shown.
Handoff tickets (a person takes over the chat) have their own queue in handoffs.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..audit import record
from ..db import get_db
from ..models import Conversation, Message, Tenant, Ticket, User
from ..security import Principal, require_staff
from .common import as_utc, get_owned, iso
from .handoffs import _deliver

router = APIRouter(prefix="/api/tickets", tags=["tickets"])
OPEN = ("open", "in_progress")


class CloseIn(BaseModel):
    resolution: str = Field(max_length=2000)


def due_at(ticket: Ticket, tenant: Tenant) -> datetime:
    if ticket.due_at is not None:
        return as_utc(ticket.due_at)
    hours = 4 if ticket.priority in ("high", "urgent") else tenant.ticket_sla_hours  # opened before 0017
    return as_utc(ticket.created_at) + timedelta(hours=hours)


def ticket_out(db: Session, ticket: Ticket, tenant: Tenant, now: datetime) -> dict:
    customer = db.get(User, ticket.customer_id)
    assignee = db.get(User, ticket.assignee_id) if ticket.assignee_id else None
    due = due_at(ticket, tenant)
    return {
        "id": ticket.id, "title": ticket.title, "description": ticket.description, "priority": ticket.priority,
        "status": ticket.status, "conversation_id": ticket.conversation_id, "resolution": ticket.resolution,
        "customer": {"id": customer.id, "name": customer.name} if customer else None,
        "assignee": {"id": assignee.id, "name": assignee.name} if assignee else None,
        "created_at": iso(ticket.created_at), "due_at": due.isoformat(), "closed_at": iso(ticket.closed_at),
        "overdue": ticket.status in OPEN and now > due,
    }


def open_ticket_stats(db: Session, tenant: Tenant) -> dict:
    """For the dashboard: open issue tickets and how many are past their promised time."""
    now = datetime.now(timezone.utc)
    tickets = db.execute(select(Ticket).where(Ticket.tenant_id == tenant.id, Ticket.kind == "issue",
                                              Ticket.status.in_(OPEN))).scalars().all()
    return {"open": len(tickets), "overdue": sum(1 for t in tickets if now > due_at(t, tenant))}


@router.get("")
def list_tickets(status_filter: str = "open", principal: Principal = Depends(require_staff),
                 db: Session = Depends(get_db)) -> list[dict]:
    """Open tickets by the time promised to the customer; ``status_filter=all`` adds the 100 most
    recently closed. Two queries, so a long closed history can never push an open ticket off the page."""
    query = select(Ticket).where(Ticket.tenant_id == principal.tenant.id, Ticket.kind == "issue")
    now = datetime.now(timezone.utc)
    live = db.execute(query.where(Ticket.status.in_(OPEN)).order_by(Ticket.id).limit(500)).scalars()
    rows = sorted((ticket_out(db, t, principal.tenant, now) for t in live), key=lambda t: t["due_at"])
    if status_filter == "all":
        done = db.execute(query.where(Ticket.status == "closed")
                          .order_by(Ticket.closed_at.desc(), Ticket.id.desc()).limit(100)).scalars()
        rows += [ticket_out(db, t, principal.tenant, now) for t in done]
    return rows


def _move(db: Session, principal: Principal, ticket_id: int, allowed: tuple[str, ...], **values) -> Ticket:
    """One conditional UPDATE, so two people acting on the same ticket cannot both succeed."""
    ticket = get_owned(db, Ticket, ticket_id, principal.tenant.id)
    if ticket.kind != "issue":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资源不存在")
    moved = db.execute(update(Ticket).where(Ticket.id == ticket.id, Ticket.status.in_(allowed)).values(**values)
                       .execution_options(synchronize_session=False)).rowcount
    if moved != 1:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "工单状态已变化，请刷新后再操作")
    return ticket


@router.post("/{ticket_id}/claim")
def claim(ticket_id: int, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    ticket = _move(db, principal, ticket_id, ("open",), status="in_progress", assignee_id=principal.user.id)
    record(db, principal, "ticket.claim", f"ticket:{ticket.id}",
           overdue=datetime.now(timezone.utc) > due_at(ticket, principal.tenant))
    db.commit()
    db.refresh(ticket)
    return ticket_out(db, ticket, principal.tenant, datetime.now(timezone.utc))


@router.post("/{ticket_id}/close")
def close(ticket_id: int, body: CloseIn, principal: Principal = Depends(require_staff),
          db: Session = Depends(get_db)) -> dict:
    """Close with what was done; the customer reads it in the conversation the ticket came from."""
    resolution = body.resolution.strip()
    if not resolution:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请填写处理结果：顾客会在对话里看到它")
    now = datetime.now(timezone.utc)
    # Closing an unclaimed ticket still records who handled it.
    ticket = _move(db, principal, ticket_id, OPEN, status="closed", closed_at=now, resolution=resolution,
                   assignee_id=func.coalesce(Ticket.assignee_id, principal.user.id))
    conversation = db.get(Conversation, ticket.conversation_id) if ticket.conversation_id else None
    text = f"您的工单 #{ticket.id} 已处理：{resolution}"
    if conversation is not None:
        db.add(Message(tenant_id=ticket.tenant_id, conversation_id=conversation.id, role="agent", content=text,
                       meta={"agent": principal.user.name, "agent_id": principal.user.id, "ticket_id": ticket.id}))
        conversation.updated_at = now
    # Whether the promise was kept; the resolution itself stays on the ticket, under retention.
    record(db, principal, "ticket.close", f"ticket:{ticket.id}", overdue=now > due_at(ticket, principal.tenant))
    db.commit()
    db.refresh(ticket)
    if conversation is not None:
        _deliver(conversation, text)
    return ticket_out(db, ticket, principal.tenant, now)
