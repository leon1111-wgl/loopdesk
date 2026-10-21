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
ERASED = "[应用户要求已删除]"  # erasure is not expiry: whoever reads the record later must see why it is gone
CLEARED = (REDACTED, ERASED)
TICKET_TEXT_REDACTED = {"title": REDACTED, "description": REDACTED, "resolution": REDACTED}
TICKET_TEXT_ERASED = {"title": ERASED, "description": ERASED, "resolution": ERASED}
# Approval arguments that are accounting data; every other value is whatever the model copied in
# from the conversation (a refund's "reason" is usually the customer's message, name and address
# included: masking only covers phone, ID and card numbers).
APPROVAL_ACCOUNTING_KEYS = ("order_id", "amount")
UNSETTLED_REFUND = ("claiming", "pending", "processing")
# Rows whose request may be sent to the payment system again (the gateway's retries, a retried
# approval, the reconciliation's resend): one idempotency key, so the body must not change. A
# "processing" row is only ever looked up by its external id, never sent again.
RESENDABLE_REFUND = ("claiming", "pending")


def apply_retention(db: Session, days: int, dry_run: bool = False) -> dict[str, int]:
    if days < 1:
        raise ValueError("days must be >= 1")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    awaiting = select(Approval.id).where(Approval.run_id == Run.id, Approval.status == "pending").exists()
    old_runs = select(Run.id).where(Run.created_at < cutoff, Run.status != "waiting_approval", ~awaiting)
    old_tickets = (Ticket.status == "closed", Ticket.closed_at < cutoff, Ticket.title.not_in(CLEARED))
    # Text an erasure already removed keeps its marker: expiry must not relabel why it is gone.
    old_messages = (Message.created_at < cutoff, Message.content.not_in(CLEARED))
    counts = {
        "spans": len(db.execute(select(Span.id).where(Span.run_id.in_(old_runs))).all()),
        "runs": len(db.execute(old_runs.where(Run.input_text.not_in(CLEARED))).all()),
        "messages": len(db.execute(select(Message.id).where(*old_messages)).all()),
        "tickets": len(db.execute(select(Ticket.id).where(*old_tickets)).all()),
        "answer_cache": len(db.execute(select(AnswerCache.id).where(AnswerCache.expires_at < now)).all()),
    }
    if dry_run:
        return counts
    db.execute(delete(Span).where(Span.run_id.in_(old_runs)))
    db.execute(update(Run).where(Run.id.in_(old_runs), Run.input_text.not_in(CLEARED))
               .values(input_text=REDACTED, output_text=REDACTED, state={}), execution_options={"synchronize_session": False})
    db.execute(update(Message).where(*old_messages).values(content=REDACTED, meta={}),
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
    unsettled = select(Refund.ticket_id).where(Refund.ticket_id.is_not(None), Refund.status.in_(UNSETTLED_REFUND))
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
    records with only their accounting fields (N-104): the approval evidence and every argument
    but order and amount, the refunds' reasons, and the eval cases and unapplied knowledge patches
    drafted from their feedback lose the text. A refund that may still be sent again (claiming,
    pending) keeps its reason: every retry and the reconciliation must send the payment system the
    same request; erasing again once it is answered clears it. ``kb_patches_applied`` counts
    corrections already merged into a knowledge document: the admin edits that document.
    Everything cleared reads ERASED, not the retention marker.
    ``principal`` is the admin doing it: recorded as the one who withdrew the approvals, with
    audit entries."""
    import secrets

    from ..models import Conversation, EvalCase, EvalResult, Feedback, KBPatch, User
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
    feedback_ids = select(Feedback.id).where(Feedback.message_id.in_(message_ids))
    # A cached answer is keyed by the question's text and not by who asked: the entries their questions
    # made go (another customer asking the same thing just misses the cache once).
    from ..agent.answer_cache import normalize_question
    asked = sorted({normalize_question(text) for (text,) in db.execute(
        select(Run.input_text).where(Run.user_id == user_id).union(
            select(Message.content).where(Message.conversation_id.in_(conversation_ids), Message.role == "user"))).all()})
    cached = 0
    for i in range(0, len(asked), 500):  # bounded bind parameters, however talkative the customer was
        cached += db.execute(delete(AnswerCache).where(AnswerCache.tenant_id == user.tenant_id,
                                                       AnswerCache.question.in_(asked[i:i + 500]))).rowcount
    # The ticket an unsettled refund points at stays open (see _close_open_items) and its text is the
    # reconciliation's instructions to staff (idempotency key, what to call), not the customer's words.
    refund_tickets = select(Refund.ticket_id).where(Refund.ticket_id.is_not(None), Refund.status.in_(UNSETTLED_REFUND))
    approvals = 0
    for approval in db.execute(select(Approval).where(Approval.run_id.in_(run_ids))).scalars():
        args = approval.arguments or {}
        approval.arguments = {k: v if k in APPROVAL_ACCOUNTING_KEYS else ERASED for k, v in args.items()}
        approval.evidence = []
        approvals += 1
    db.flush()
    counts = {
        "messages": db.execute(update(Message).where(Message.conversation_id.in_(conversation_ids))
                               .values(content=ERASED, meta={}), execution_options={"synchronize_session": False}).rowcount,
        "spans": db.execute(delete(Span).where(Span.run_id.in_(run_ids))).rowcount,
        "runs": db.execute(update(Run).where(Run.user_id == user_id)
                           .values(input_text=ERASED, output_text=ERASED, state={}),
                           execution_options={"synchronize_session": False}).rowcount,
        # A flywheel case or a patch nobody reviewed yet quotes their question; it is archived (a
        # case without its question tests nothing), with the answers its eval runs recorded. A patch
        # already applied is now part of a knowledge document and is left to the admin to edit.
        "eval_cases": db.execute(update(EvalCase).where(EvalCase.feedback_id.in_(feedback_ids))
                                 .values(question=ERASED, history=None, as_user=None, status="archived"),
                                 execution_options={"synchronize_session": False}).rowcount,
        "eval_answers": db.execute(update(EvalResult).where(EvalResult.case_id.in_(
            select(EvalCase.id).where(EvalCase.feedback_id.in_(feedback_ids)))).values(answer=ERASED),
            execution_options={"synchronize_session": False}).rowcount,
        "kb_patches": db.execute(update(KBPatch).where(KBPatch.feedback_id.in_(feedback_ids), KBPatch.status != "applied")
                                 .values(title=ERASED, content=ERASED, status="rejected"),
                                 execution_options={"synchronize_session": False}).rowcount,
        "feedback": db.execute(update(Feedback).where(Feedback.id.in_(feedback_ids))
                               .values(question=ERASED, answer=ERASED, correction=ERASED, comment=""),
                               execution_options={"synchronize_session": False}).rowcount,
        "approvals": approvals,
        "answer_cache": cached,
        "refunds": db.execute(update(Refund).where(Refund.customer_id == user_id, Refund.status.not_in(RESENDABLE_REFUND))
                              .values(reason=ERASED), execution_options={"synchronize_session": False}).rowcount,
        "refunds_unsettled": len(db.execute(select(Refund.id).where(
            Refund.customer_id == user_id, Refund.status.in_(RESENDABLE_REFUND), Refund.reason.not_in(("", ERASED)))).all()),
        "kb_patches_applied": len(db.execute(select(KBPatch.id).where(KBPatch.feedback_id.in_(feedback_ids),
                                                                      KBPatch.status == "applied")).all()),
        "tickets": db.execute(update(Ticket).where(Ticket.customer_id == user_id, Ticket.id.not_in(refund_tickets))
                              .values(**TICKET_TEXT_ERASED),
                              execution_options={"synchronize_session": False}).rowcount,
        "approvals_withdrawn": len(withdrawn),
        "tickets_closed": len(closed),
    }
    if closed:  # the reason it was closed is not the customer's data; it stays readable
        db.execute(update(Ticket).where(Ticket.id.in_(closed)).values(resolution=ERASED_TICKET_RESOLUTION),
                   execution_options={"synchronize_session": False})
    db.execute(update(Conversation).where(Conversation.user_id == user_id).values(title=ERASED),
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
