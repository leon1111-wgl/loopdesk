"""Human takeover: one handoff ticket per conversation, the waiting queue, and hand-back.

While a person owns a conversation the agent stays out of it: the customer's messages are
stored for that person, the customer is told where they stand, and no model is called (so
the handoff rate and the token bill are not inflated by "are you there?" messages). Staff
reply from the handoff queue and hand the conversation back to the agent when done.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Conversation, Message, Ticket, User
from ..textutil import NEGATED_PERSON_RE, ORDER_ID_RE, normalize_width

ACTIVE = ("open", "in_progress")  # open: waiting for a person; in_progress: a person replied


# What the customer wants, in words a person scans in a queue (N-058). Only labels for the card:
# nothing is decided on them, so a missed intent costs a glance at the transcript, not a refund.
INTENTS = (
    ("退款", re.compile(r"退款|退钱|退费|把钱退")),
    ("退货", re.compile(r"退货|退回去")),
    ("换货", re.compile(r"换货|换个新|换一个新|给我换一(?:个|台|件)")),
    ("投诉", re.compile(r"投诉")),
    ("商品问题", re.compile(r"碎了|破了|坏[了的]|破损|没声音|不能用|用不了|质量(?:问题|差|不好|太差)")),
    # "退款还没到账" / "没收到退款" are about money, not a parcel.
    ("物流", re.compile(r"快递|物流|发货|没到(?!账)|没收到(?!退款|钱)")),
    ("发票", re.compile(r"发票")),
    ("账号", re.compile(r"账号|密码|登录|登不上")),
)
# "我不是要退款，是要换货" asks for the exchange only; the person rule's negations ("不是要投诉，
# 转人工") are cut too.
NEGATED_INTENT_RE = re.compile(r"(?:不是要?|不想要?|不要|不需要|不用|别(?:给我)?)(?:申请|开)?(?:退款|退货|换货|投诉|发票)")
QUOTE_LIMIT = 120
TITLE_LIMIT = 60


def brief(reason: str, customer_text: str, earlier_orders: list[str] | tuple[str, ...] = ()) -> tuple[str, str]:
    """Title and description of a handoff ticket: why it was opened, the order numbers and
    intents found in what the customer wrote, and their words (already PII-masked), so the
    person who picks it up does not have to ask again. A model's reason or a fixed rule reason
    alone ("顾客要求转人工") told staff nothing. `earlier_orders` are the order numbers of the
    conversation's previous turns: "太慢了，转人工" after "SO… 到哪了" is about that order."""
    text = normalize_width(customer_text or "").strip()
    orders = list(dict.fromkeys([m.upper() for m in ORDER_ID_RE.findall(text)] + [o.upper() for o in earlier_orders]))
    wanted = NEGATED_INTENT_RE.sub("，", NEGATED_PERSON_RE.sub("，", text))
    intents = [name for name, pattern in INTENTS if pattern.search(wanted)]
    suffix = f" · {orders[0]}" if orders else ""
    head = ("、".join(intents) or reason)[:TITLE_LIMIT - len("转人工：") - len(suffix)]
    lines = [f"原因：{reason}"]
    if orders:
        lines.append(f"订单：{'、'.join(orders)}")
    if intents:
        lines.append(f"诉求：{'、'.join(intents)}")
    if text:
        quote = text if len(text) <= QUOTE_LIMIT else text[:QUOTE_LIMIT] + "…"
        lines.append(f"顾客原话：{quote}")
    return f"转人工：{head}{suffix}", "\n".join(lines)


def earlier_orders(db: Session, conversation_id: int | None, turns: int = 6) -> list[str]:
    """Order numbers in the conversation's recent customer messages, newest first, back to the
    last hand-back (a finished handoff's order is another topic). Only the numbers are taken
    (they are not PII); the quote stays the current, masked turn."""
    if conversation_id is None:
        return []
    rows = db.execute(
        select(Message.role, Message.content).where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc()).limit(turns * 2)
    ).all()
    orders: list[str] = []
    for role, content in rows:
        if role == "system":  # the hand-back notice
            break
        if role == "user":
            orders += [m.upper() for m in ORDER_ID_RE.findall(normalize_width(content or ""))]
    return orders


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
