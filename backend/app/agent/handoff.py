"""Human takeover: one handoff ticket per conversation, the waiting queue, and hand-back.

While a person owns a conversation the agent stays out of it: the customer's messages are
stored for that person, the customer is told where they stand, and no model is called (so
the handoff rate and the token bill are not inflated by "are you there?" messages). Staff
reply from the handoff queue and hand the conversation back to the agent when done.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Conversation, Ticket, User

ACTIVE = ("open", "in_progress")  # open: waiting for a person; in_progress: a person replied


def active_ticket(db: Session, conversation_id: int) -> Ticket | None:
    return db.execute(
        select(Ticket).where(Ticket.conversation_id == conversation_id, Ticket.kind == "handoff",
                             Ticket.status.in_(ACTIVE)).order_by(Ticket.id.desc())
    ).scalars().first()


def waiting_ahead(db: Session, tenant_id: int, before_id: int | None = None) -> int:
    """Tickets nobody has picked up yet, optionally only those created before a given one."""
    query = select(func.count(Ticket.id)).where(
        Ticket.tenant_id == tenant_id, Ticket.kind == "handoff", Ticket.status == "open")
    if before_id is not None:
        query = query.where(Ticket.id < before_id)
    return db.execute(query).scalar_one()


def queue_position(db: Session, ticket: Ticket) -> int | None:
    """1-based place in the queue, or None once a person is on it."""
    return waiting_ahead(db, ticket.tenant_id, ticket.id) + 1 if ticket.status == "open" else None


def ticket_state(db: Session, ticket: Ticket) -> dict:
    assignee = db.get(User, ticket.assignee_id) if ticket.assignee_id else None
    return {"ticket_id": ticket.id, "status": ticket.status, "position": queue_position(db, ticket),
            "assignee": assignee.name if assignee else None}


def state(db: Session, conversation: Conversation) -> dict | None:
    ticket = active_ticket(db, conversation.id) if conversation.status == "handoff" else None
    return ticket_state(db, ticket) if ticket else None


def notice(current: dict | None) -> str:
    if current and current["assignee"]:
        return f"人工客服 {current['assignee']} 正在为您服务，您的消息已转给对方。"
    if current and current["position"]:
        return (f"已为您转接人工客服，当前排队第 {current['position']} 位。"
                "您可以继续留言，人工客服接入后会在这里回复。")
    return "已为您转接人工客服，请稍候。"
