"""Data retention: keep personal conversation data only as long as needed.

Old rows are redacted rather than deleted: approvals, refunds and feedback reference runs
and messages (audit and flywheel data must survive), so deleting would either break foreign
keys or lose the audit trail. After ``days``:
- spans of finished runs are deleted (bulk of the trace payload);
- run input/output text and state are replaced by a marker; metrics (tokens, cost,
  latency, verdict) stay for reporting;
- message bodies and metadata are replaced by the marker;
- tickets closed before the cutoff keep their status, priority and times, but their text (title
  and description, the customer's words, and the resolution written to them) is replaced; open
  tickets are left alone, someone still has to act on them;
- expired answer-cache entries are deleted.
Runs still waiting for an approval are never touched, nor are runs whose approval went back to
pending after a failed execution (the run is "failed", the decision still needs its state).

    python -m app.ops.retention --days 90 --dry-run
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..models import AnswerCache, Approval, Message, Refund, Run, Span, Ticket

REDACTED = "[已过保留期，内容已清除]"
TICKET_TEXT_REDACTED = {"title": REDACTED, "description": REDACTED, "resolution": REDACTED}


def apply_retention(db: Session, days: int, dry_run: bool = False) -> dict[str, int]:
    if days < 1:
        raise ValueError("days must be >= 1")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    awaiting = select(Approval.id).where(Approval.run_id == Run.id, Approval.status == "pending").exists()
    old_runs = select(Run.id).where(Run.created_at < cutoff, Run.status != "waiting_approval", ~awaiting)
    old_tickets = (Ticket.status == "closed", Ticket.closed_at < cutoff, Ticket.title != REDACTED)
    counts = {
        "spans": len(db.execute(select(Span.id).where(Span.run_id.in_(old_runs))).all()),
        "runs": len(db.execute(old_runs.where(Run.input_text != REDACTED)).all()),
        "messages": len(db.execute(select(Message.id).where(Message.created_at < cutoff, Message.content != REDACTED)).all()),
        "tickets": len(db.execute(select(Ticket.id).where(*old_tickets)).all()),
        "answer_cache": len(db.execute(select(AnswerCache.id).where(AnswerCache.expires_at < now)).all()),
    }
    if dry_run:
        return counts
    db.execute(delete(Span).where(Span.run_id.in_(old_runs)))
    db.execute(update(Run).where(Run.id.in_(old_runs)).values(input_text=REDACTED, output_text=REDACTED, state={}),
               execution_options={"synchronize_session": False})
    db.execute(update(Message).where(Message.created_at < cutoff).values(content=REDACTED, meta={}),
               execution_options={"synchronize_session": False})
    db.execute(update(Ticket).where(*old_tickets).values(**TICKET_TEXT_REDACTED),
               execution_options={"synchronize_session": False})
    db.execute(delete(AnswerCache).where(AnswerCache.expires_at < now))
    db.commit()
    return counts


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wrapper
    from ..config import get_settings
    from ..db import init_engine, new_session

    parser = argparse.ArgumentParser(description="LoopDesk data retention")
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    init_engine(get_settings().database_url)
    with new_session() as db:
        counts = apply_retention(db, args.days, args.dry_run)
    print(("would redact/delete: " if args.dry_run else "redacted/deleted: ") + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


ERASED_APPROVAL_COMMENT = "顾客已申请删除个人数据，申请自动撤销"
ERASED_TICKET_RESOLUTION = "顾客已申请删除个人数据，工单随之关闭"


class RunInFlight(Exception):
    """The customer has a run executing right now: it would write their words back after erasure."""


def _close_open_items(db: Session, user_id: int, principal) -> tuple[list[dict], list[int]]:
    """What the customer left unfinished is closed before their words go (N-103): erasure clears
    the paused run's state, so a pending approval could never be decided again (every decision
    failed and reopened it), and their handoffs kept places in the queue that nobody could serve.
    An approval is withdrawn, not decided: no money moves. Returns the audit entries to record
    (last, after every row: the tenant row's audit counter is the last lock, as everywhere else)
    and the closed ticket ids. A ticket an unsettled refund still points at stays open: someone has
    to confirm that money with the payment system, erased customer or not."""
    from ..models import Conversation

    now = datetime.now(timezone.utc)
    reviewer_id = principal.user.id if principal is not None else None
    run_ids = select(Run.id).where(Run.user_id == user_id)
    pending = db.execute(select(Approval.id, Approval.run_id, Approval.tool_name, Approval.arguments)
                         .where(Approval.run_id.in_(run_ids), Approval.status == "pending")).all()
    withdrawn = []
    for approval_id, run_id, tool_name, args in pending:
        # Conditional, like a reviewer's claim: an approval decided meanwhile is left as decided.
        if db.execute(update(Approval).where(Approval.id == approval_id, Approval.status == "pending")
                      .values(status="rejected", reviewer_id=reviewer_id, comment=ERASED_APPROVAL_COMMENT,
                              decided_at=now), execution_options={"synchronize_session": False}).rowcount != 1:
            continue
        db.execute(update(Run).where(Run.id == run_id, Run.status == "waiting_approval")
                   .values(status="completed", finished_at=now), execution_options={"synchronize_session": False})
        args = args or {}
        withdrawn.append({"target": f"approval:{approval_id}", "tool": tool_name, "order_id": args.get("order_id"),
                          "amount": args.get("amount"), "comment": ERASED_APPROVAL_COMMENT})
    unsettled = select(Refund.ticket_id).where(Refund.ticket_id.is_not(None),
                                               Refund.status.in_(("claiming", "pending", "processing")))
    open_tickets = select(Ticket.id).where(Ticket.customer_id == user_id, Ticket.status.in_(("open", "in_progress")),
                                           Ticket.id.not_in(unsettled))
    closed = [row[0] for row in db.execute(open_tickets).all()]
    if closed:
        db.execute(update(Ticket).where(Ticket.id.in_(closed))
                   .values(status="closed", closed_at=now, assignee_id=func.coalesce(Ticket.assignee_id, reviewer_id)),
                   execution_options={"synchronize_session": False})
        db.execute(update(Conversation).where(Conversation.user_id == user_id, Conversation.status == "handoff")
                   .values(status="bot"), execution_options={"synchronize_session": False})
    return withdrawn, closed


def erase_user(db: Session, user_id: int, principal=None) -> dict[str, int]:
    """Right to erasure for one user: close what they left open (pending approvals are withdrawn,
    queued handoffs and tickets closed), redact their conversations, runs, the text of their
    tickets and the feedback written about their messages, drop trace spans, anonymise the
    account so it can no longer sign in. Orders, refunds and approvals stay as accounting/audit
    records, but the approval evidence (which quotes the conversation) is cleared. ``principal``
    is the admin doing it: recorded as the one who withdrew the approvals, with audit entries."""
    import secrets

    from ..models import Conversation, Feedback, User
    from ..security import hash_password

    user = db.get(User, user_id)
    if user is None:
        raise ValueError("user not found")
    if db.execute(select(Run.id).where(Run.user_id == user_id, Run.status == "running").limit(1)).first():
        # A run (or an approval being carried out) saves its state and answer when it ends, which
        # would put the customer's words back after the erasure; and an approval claimed but not yet
        # executed could no longer be put back by the stale-run scan once its state is gone.
        raise RunInFlight("该用户有正在执行的对话或审批，请稍后重试")
    withdrawn, closed = _close_open_items(db, user_id, principal)
    conversation_ids = select(Conversation.id).where(Conversation.user_id == user_id)
    message_ids = select(Message.id).where(Message.conversation_id.in_(conversation_ids))
    run_ids = select(Run.id).where(Run.user_id == user_id)
    counts = {
        "messages": db.execute(update(Message).where(Message.conversation_id.in_(conversation_ids))
                               .values(content=REDACTED, meta={}), execution_options={"synchronize_session": False}).rowcount,
        "spans": db.execute(delete(Span).where(Span.run_id.in_(run_ids))).rowcount,
        "runs": db.execute(update(Run).where(Run.user_id == user_id)
                           .values(input_text=REDACTED, output_text=REDACTED, state={}),
                           execution_options={"synchronize_session": False}).rowcount,
        "feedback": db.execute(update(Feedback).where(Feedback.message_id.in_(message_ids))
                               .values(question=REDACTED, answer=REDACTED, correction=REDACTED, comment=""),
                               execution_options={"synchronize_session": False}).rowcount,
        "approvals": db.execute(update(Approval).where(Approval.run_id.in_(run_ids)).values(evidence=[]),
                                execution_options={"synchronize_session": False}).rowcount,
        "tickets": db.execute(update(Ticket).where(Ticket.customer_id == user_id).values(**TICKET_TEXT_REDACTED),
                              execution_options={"synchronize_session": False}).rowcount,
        "approvals_withdrawn": len(withdrawn),
        "tickets_closed": len(closed),
    }
    if closed:  # the reason it was closed is not the customer's data; it stays readable
        db.execute(update(Ticket).where(Ticket.id.in_(closed)).values(resolution=ERASED_TICKET_RESOLUTION),
                   execution_options={"synchronize_session": False})
    db.execute(update(Conversation).where(Conversation.user_id == user_id).values(title=REDACTED),
               execution_options={"synchronize_session": False})
    user.name = f"已注销用户 {user.id}"
    user.email = f"erased-{user.id}-{secrets.token_hex(4)}@invalid"
    user.password_hash = hash_password(secrets.token_urlsafe(32))
    user.token_version += 1  # tokens issued before the erasure stop working now, not in 12 hours
    if principal is not None:
        from .. import audit
        for entry in withdrawn:
            audit.record(db, principal, "approval.reject", entry.pop("target"), **entry)
    db.commit()
    return counts
