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


# A complaint about a member of staff (N-222): "我要投诉你们的客服Mia 态度太差了", "刚才那个客服太敷衍了". The ticket
# names them; they cannot take it or close it, and a complaint about an agent is an administrator's.
# Two kinds of complaint words: one names its object after it ("投诉你们的客服 Mia"), the other says what the person
# before it did ("Mia 态度太差了", "客服Mia太敷衍"). A name counts only in that place: "Mia 我要投诉物流" and
# "谢谢Mia 不过我要投诉快递" are not about her (second-model review).
_ACCUSE_RE = re.compile(r"投诉|举报|(?<![A-Za-z])(?:complain(?:t|ed)?\s+(?:about\s+)?|report)", re.IGNORECASE)
_TRAIT_RE = re.compile(r"态度(?:太|很|真|特别|非常|极其)?(?:差|恶劣|不好|烂|恶心)|骂人|骂我|辱骂|不耐烦|敷衍"
                       r"|(?<![A-Za-z])(?:rude|impolite)", re.IGNORECASE)
_COMPLAINT_RE = re.compile(f"{_ACCUSE_RE.pattern}|{_TRAIT_RE.pattern}", re.IGNORECASE)
# "你态度太差了 我要投诉" while a person holds the handoff: "you" is them ("你们" is the shop).
_YOU_RE = re.compile(r"(?:投诉|举报)(?:一下)?(?:你|您)(?!们)|(?:你|您)(?!们)(?:的)?(?:服务)?(?:态度)?.{0,6}?"
                     r"(?:态度(?:太|很|真)?(?:差|恶劣|不好)|敷衍|不耐烦|骂人|骂我)")
# "我不是要投诉Mia 我要投诉快递": the person a negated complaint names goes with it.
_NOT_COMPLAINING_RE = re.compile(r"(?:不(?:是要?|想|要|用|会|再)?|没(?:有)?要?|别)投诉\s*[A-Za-z]*[^，,。！!？?\s]*")
_EARLIER_AGENT_RE = re.compile(r"(?:刚才|刚刚|之前|上一个|上个|前面)的?(?:那个|那位|这个|这位)?(?:人工)?(?:客服|坐席)")
_STAFF_CUE = r"(?:客服|坐席|人工|管理员|店长|经理|员工|工作人员|小二)"
_GENERIC_NAMES = {"客服", "人工", "坐席", "管理员", "店长", "经理", "员工", "工作人员", "小二", "机器人"}
# A clause: the name and the complaint must be in the same one ("Mia帮我处理的退款还没到 我要投诉物流" is about logistics).
# A space between two Chinese characters ends one; "我要投诉你们的客服 Mia" and "客服Mia 态度太差了" are still one.
_CLAUSE_SPLIT_RE = re.compile(r"[，,。；;！!？?\n]+|(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
_AFTER_ACCUSE = 12  # "投诉你们的客服 Mia": characters between the complaint word and the name
_BEFORE_TRAIT = 8  # "客服Mia 的态度太差了"
# "Mia说可以在这里投诉", "Mia帮我处理的": she said or did something; the complaint is not about her.
_NOT_THE_OBJECT_RE = re.compile(r"\s*(?:说|讲|告诉|帮|让我|叫我)")
STAFF_ROLES = ("agent", "admin")


def _name_matches(text: str, name: str) -> list[re.Match[str]]:
    name = (name or "").strip()
    if len(name) < 2 or name in _GENERIC_NAMES:
        return []
    if name.isascii():  # "客服Mia", "Mia's attitude"; not "Miami"
        return list(re.finditer(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", text, re.IGNORECASE))
    return list(re.finditer(re.escape(name), text))


def _brand(tenant) -> tuple[re.Pattern[str] | None, set[str]]:
    """The shop's own name, any case or spacing ("leon优选", "Leon 优选"), and its words a staff name may share."""
    name = (getattr(tenant, "name", "") or "").strip()
    runs = re.findall(r"[A-Za-z0-9]+|[^A-Za-z0-9\s]+", name)
    pattern = re.compile(r"\s*".join(map(re.escape, runs)), re.IGNORECASE) if runs else None
    words = {w.lower() for w in runs if w.isascii()} | {(getattr(tenant, "slug", "") or "").lower()}
    return pattern, words - {""}


def _cued(clause: str, match: re.Match[str]) -> bool:
    return bool(re.search(_STAFF_CUE + r".{0,2}$", clause[:match.start()])
                or re.match(r"^.{0,2}" + _STAFF_CUE, clause[match.end():]))


def _someone_else(db: Session, tenant_id: int, subject: User) -> bool:
    """Whether anyone could handle a complaint about ``subject``: an administrator for an agent, any other member of
    staff for an administrator. A one-person shop's complaint is not locked away from its only administrator."""
    roles = ("admin",) if subject.role == "agent" else STAFF_ROLES
    return db.execute(select(User.id).where(User.tenant_id == tenant_id, User.id != subject.id, User.role.in_(roles),
                                            User.disabled_at.is_(None)).limit(1)).first() is not None


def complaint_subject(db: Session, tenant, customer: User, conversation_id: int | None, customer_text: str) -> User | None:
    """The member of staff a complaint is about: named in the clause that complains, nearest to the complaint word,
    or "刚才那个客服" when a person replied in this conversation. The shop's own name is cut first ("投诉Leon优选" is
    not about Leon the administrator), and a staff name that is also a word of the shop's name ("你们Leon家的快递")
    counts only next to 客服 / 管理员 / …. None when nobody else could handle it."""
    text = NEGATED_INTENT_RE.sub("，", _NOT_COMPLAINING_RE.sub("，", normalize_width(customer_text or "")))
    brand, brand_words = _brand(tenant)
    if brand is not None:
        text = brand.sub("", text)  # not a space: "投诉leon优选的客服mia" stays one clause
    staff = [u for u in db.execute(select(User).where(User.tenant_id == tenant.id, User.role.in_(STAFF_ROLES))
                                   .order_by(User.id)).scalars()
             if u.id != customer.id and u.name != customer.name]
    subject = None
    for clause in _CLAUSE_SPLIT_RE.split(text):
        if not _COMPLAINT_RE.search(clause):
            continue
        near = []
        for user in staff:
            for m in _name_matches(clause, user.name):
                if ((user.name.strip().lower() in brand_words and not _cued(clause, m))
                        or _NOT_THE_OBJECT_RE.match(clause, m.end())):
                    continue
                gaps = [m.start() - a.end() for a in _ACCUSE_RE.finditer(clause) if 0 <= m.start() - a.end() <= _AFTER_ACCUSE]
                gaps += [t.start() - m.end() for t in _TRAIT_RE.finditer(clause) if 0 <= t.start() - m.end() <= _BEFORE_TRAIT]
                if gaps:
                    near.append((min(gaps), user.id, user))
        if near:
            subject = min(near)[2]
            break
        if subject is None and conversation_id is not None and _EARLIER_AGENT_RE.search(clause):
            meta = db.execute(select(Message.meta).where(Message.conversation_id == conversation_id, Message.role == "agent")
                              .order_by(Message.id.desc()).limit(1)).scalar_one_or_none()
            agent_id = (meta or {}).get("agent_id")
            user = db.get(User, agent_id) if agent_id else None
            if user is not None and user.tenant_id == tenant.id and user.role in STAFF_ROLES:
                subject = user
                break
    return subject if subject is not None and _someone_else(db, tenant.id, subject) else None


def subject_note(subject: User) -> str:
    return f"涉及员工：{subject.name}（本人不能接手或结束，{'由其他同事处理' if subject.role == 'admin' else '由管理员处理'}）"


def name_subject(db: Session, tenant, customer: User, ticket: Ticket, customer_text: str) -> User | None:
    """A complaint made after the handoff began ("我要投诉你 Mia" while Mia holds it): the live ticket names her from
    now on, and goes back to the queue if she held it. Set once, in one conditional UPDATE."""
    if ticket.subject_staff_id is not None:
        return None
    subject = complaint_subject(db, tenant, customer, ticket.conversation_id, customer_text)
    if subject is None and ticket.assignee_id is not None and _YOU_RE.search(normalize_width(customer_text or "")):
        held = db.get(User, ticket.assignee_id)  # "你态度太差了 我要投诉" to the person serving them
        subject = held if held is not None and _someone_else(db, tenant.id, held) else None
    if subject is None:
        return None
    from sqlalchemy import case, update

    released = db.execute(update(Ticket).where(Ticket.id == ticket.id, Ticket.subject_staff_id.is_(None),
                                               Ticket.status.in_(ACTIVE))
                          .values(subject_staff_id=subject.id, description=Ticket.description + "\n" + subject_note(subject),
                                  assignee_id=case((Ticket.assignee_id == subject.id, None), else_=Ticket.assignee_id),
                                  status=case((Ticket.assignee_id == subject.id, "open"), else_=Ticket.status))
                          .execution_options(synchronize_session="fetch")).rowcount
    return subject if released == 1 else None


def may_handle(db: Session, ticket: Ticket, user: User) -> str | None:
    """Why ``user`` may not take, answer or close ``ticket``, or None."""
    if ticket.subject_staff_id is None:
        return None
    if ticket.subject_staff_id == user.id:
        return "这条投诉涉及您本人，请由管理员处理"
    subject = db.get(User, ticket.subject_staff_id)
    if user.role != "admin" and (subject is None or subject.role != "admin"):
        return "涉及员工的投诉需要管理员处理"
    return None


CANCELLED_TEXT = "好的，已为您取消人工排队。还有其他问题可以继续问我。"
CANCELLED_NOTE = "顾客取消了人工排队，会话交还智能客服。"


def cancel_by_customer(db: Session, ticket: Ticket, conversation: Conversation) -> bool:
    """The customer left the queue before anyone took the ticket (N-062): closed as theirs, the agent answers
    again. False when a person picked it up in the meantime: then the message is theirs."""
    from datetime import datetime, timezone

    from sqlalchemy import update

    closed = db.execute(update(Ticket).where(Ticket.id == ticket.id, Ticket.status == "open", Ticket.assignee_id.is_(None))
                        .values(status="closed", closed_at=datetime.now(timezone.utc), resolution=CANCELLED_NOTE)
                        .execution_options(synchronize_session="fetch")).rowcount
    if closed != 1:
        return False
    conversation.status = "bot"
    db.add(Message(tenant_id=ticket.tenant_id, conversation_id=conversation.id, role="system", content=CANCELLED_NOTE))
    return True


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
