"""CSV exports for operations teams: conversations (every message), runs, tickets, handoffs,
feedback and approvals.

- Every export is recorded in the audit log (who, what, how many rows) before the file is sent.

- UTF-8 with BOM, so Excel opens Chinese text without mojibake.
- Cells starting with = + - @ (or tab / carriage return) are prefixed with a single
  quote: user-controlled text such as a customer's question must not execute as a
  spreadsheet formula ("CSV injection").
- Tenant-scoped like every other endpoint; text fields are the already-masked versions
  stored in the database, so exports do not re-expose PII.
- Times are the tenant's local time ("2026-10-20 09:12:33") and every time column says which zone
  in its header, e.g. "created_at (Asia/Shanghai)" (N-023: they were UTC ISO strings).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import record
from ..db import get_db
from ..localtime import local_time, zone_of
from ..models import Approval, Conversation, Feedback, Message, Run, Span, Ticket, User
from ..security import Principal, rate_limiter, require_staff
from .common import as_utc
from .handoffs import first_reply, reply_times
from .tickets import due_at

router = APIRouter(prefix="/api/export", tags=["export"])
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def to_csv(header: list[str], rows: Iterable[list]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([safe_cell(v) for v in row])
    return "﻿".encode() + buffer.getvalue().encode("utf-8")


def _export(db: Session, principal: Principal, name: str, days: int, header: list[str],
            rows: Iterable[list]) -> Response:
    """Customer data leaving the system is the one read worth an audit entry (N-022). The entry
    commits before the file is handed out: no entry, no file."""
    rows = list(rows)
    payload = to_csv(header, rows)
    record(db, principal, "export.download", f"export:{name}", days=days, rows=len(rows))
    db.commit()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(payload, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="loopdesk-{name}-{stamp}.csv"'})


def _since(days: int) -> datetime:
    if not 1 <= days <= 365:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "days 必须在 1 到 365 之间")
    return datetime.now(timezone.utc) - timedelta(days=days)


@router.get("/runs.csv")
def export_runs(days: int = 30, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    rate_limiter.check(f"export:{principal.user.id}", 20)
    rows = db.execute(
        select(Run, User).join(User, User.id == Run.user_id)
        .where(Run.tenant_id == principal.tenant.id, Run.is_eval.is_(False), Run.created_at >= _since(days))
        .order_by(Run.created_at)
    ).all()
    tools: dict[str, list[str]] = {}
    if rows:
        for run_id, name in db.execute(
            select(Span.run_id, Span.name).where(Span.run_id.in_([r.id for r, _ in rows]), Span.kind == "tool").order_by(Span.id)
        ):
            tools.setdefault(run_id, []).append(name)
    zone = zone_of(principal.tenant)
    header = ["run_id", f"created_at ({zone.key})", "user", "role", "input", "output", "status", "route_tier", "model", "steps",
              "latency_ms", "prompt_tokens", "completion_tokens", "cost", "evidence_verdict", "evidence_coverage", "tools"]
    return _export(db, principal, "runs", days, header, (
        [r.id, local_time(r.created_at, zone), u.name, u.role, r.input_text, r.output_text, r.status, r.route_tier, r.model, r.steps,
         r.latency_ms, r.prompt_tokens, r.completion_tokens, r.cost, r.verdict, r.evidence_coverage,
         " > ".join(tools.get(r.id, []))]
        for r, u in rows
    ))


@router.get("/feedback.csv")
def export_feedback(days: int = 30, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    rate_limiter.check(f"export:{principal.user.id}", 20)
    rows = db.execute(
        select(Feedback, User).join(User, User.id == Feedback.user_id)
        .where(Feedback.tenant_id == principal.tenant.id, Feedback.created_at >= _since(days))
        .order_by(Feedback.created_at)
    ).all()
    zone = zone_of(principal.tenant)
    header = ["feedback_id", f"created_at ({zone.key})", "user", "rating", "question", "answer", "correction", "reason", "attribution", "run_id"]
    return _export(db, principal, "feedback", days, header, (
        [f.id, local_time(f.created_at, zone), u.name, f.rating, f.question, f.answer, f.correction, f.reason_category, f.attribution, f.run_id]
        for f, u in rows
    ))


@router.get("/approvals.csv")
def export_approvals(days: int = 30, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    rate_limiter.check(f"export:{principal.user.id}", 20)
    approvals = db.execute(
        select(Approval).where(Approval.tenant_id == principal.tenant.id, Approval.created_at >= _since(days))
        .order_by(Approval.created_at)
    ).scalars().all()
    names = {u.id: u.name for u in db.execute(select(User).where(User.tenant_id == principal.tenant.id)).scalars()}
    zone = zone_of(principal.tenant)
    header = ["approval_id", f"created_at ({zone.key})", f"decided_at ({zone.key})", "tool", "order_id", "amount", "reason", "status",
              "requested_by", "reviewer", "comment", "run_id"]
    return _export(db, principal, "approvals", days, header, (
        [a.id, local_time(a.created_at, zone), local_time(a.decided_at, zone), a.tool_name, (a.arguments or {}).get("order_id"),
         (a.arguments or {}).get("amount"), a.reason, a.status, names.get(a.requested_by),
         names.get(a.reviewer_id) if a.reviewer_id else "", a.comment, a.run_id]
        for a in approvals
    ))


@router.get("/conversations.csv")
def export_conversations(days: int = 30, principal: Principal = Depends(require_staff),
                         db: Session = Depends(get_db)) -> Response:
    """Every message in the window, in conversation order: what the customer, the agent and people
    said. Evaluation conversations are left out."""
    rate_limiter.check(f"export:{principal.user.id}", 20)
    zone = zone_of(principal.tenant)
    rows = db.execute(
        select(Message, Conversation, User).join(Conversation, Conversation.id == Message.conversation_id)
        .join(User, User.id == Conversation.user_id)
        .where(Conversation.tenant_id == principal.tenant.id, Conversation.channel != "eval",
               Message.created_at >= _since(days))
        .order_by(Conversation.id, Message.id)
    ).all()

    def author(m: Message, customer: User) -> str:
        if m.role == "user":
            return customer.name
        return str((m.meta or {}).get("agent") or "") if m.role == "agent" else ""

    header = ["conversation_id", "channel", "customer", "message_id", f"created_at ({zone.key})", "role", "author", "content"]
    return _export(db, principal, "conversations", days, header, (
        [c.id, c.channel, u.name, m.id, local_time(m.created_at, zone), m.role, author(m, u), m.content]
        for m, c, u in rows
    ))


@router.get("/tickets.csv")
def export_tickets(days: int = 30, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    """Issue tickets opened in the window, with the promise made to the customer and whether it was
    kept: sla is met / missed for closed tickets, open / overdue for the rest."""
    rate_limiter.check(f"export:{principal.user.id}", 20)
    zone, tenant = zone_of(principal.tenant), principal.tenant
    tickets = db.execute(
        select(Ticket).where(Ticket.tenant_id == tenant.id, Ticket.kind == "issue", Ticket.created_at >= _since(days))
        .order_by(Ticket.id)
    ).scalars().all()
    names = {u.id: u.name for u in db.execute(select(User).where(User.tenant_id == tenant.id)).scalars()}
    now = datetime.now(timezone.utc)

    def sla(t: Ticket) -> str:
        due = due_at(t, tenant)
        if t.status == "closed":
            return "met" if t.closed_at and as_utc(t.closed_at) <= due else "missed"
        return "overdue" if now > due else "open"

    header = ["ticket_id", f"created_at ({zone.key})", "priority", "status", "customer", "assignee",
              f"due_at ({zone.key})", f"closed_at ({zone.key})", "sla", "title", "description", "resolution",
              "conversation_id"]
    return _export(db, principal, "tickets", days, header, (
        [t.id, local_time(t.created_at, zone), t.priority, t.status, names.get(t.customer_id, ""),
         names.get(t.assignee_id, "") if t.assignee_id else "", local_time(due_at(t, tenant), zone),
         local_time(t.closed_at, zone), sla(t), t.title, t.description, t.resolution, t.conversation_id or ""]
        for t in tickets
    ))


@router.get("/handoffs.csv")
def export_handoffs(days: int = 30, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> Response:
    """Handoffs requested in the window: how long the customer waited for the first human reply
    (only replies while that handoff was open count), who served them and when it ended."""
    rate_limiter.check(f"export:{principal.user.id}", 20)
    zone, tenant_id = zone_of(principal.tenant), principal.tenant.id
    tickets = db.execute(
        select(Ticket).where(Ticket.tenant_id == tenant_id, Ticket.kind == "handoff", Ticket.created_at >= _since(days))
        .order_by(Ticket.id)
    ).scalars().all()
    names = {u.id: u.name for u in db.execute(select(User).where(User.tenant_id == tenant_id)).scalars()}
    channels = dict(db.execute(select(Conversation.id, Conversation.channel)
                               .where(Conversation.id.in_({t.conversation_id for t in tickets if t.conversation_id}))).all())
    times = reply_times(db, {t.conversation_id for t in tickets if t.conversation_id})

    def row(t: Ticket) -> list:
        reply = first_reply(t, times)
        wait = int((reply - as_utc(t.created_at)).total_seconds()) if reply else ""
        return [t.id, t.conversation_id or "", channels.get(t.conversation_id, ""), names.get(t.customer_id, ""),
                local_time(t.created_at, zone), local_time(reply, zone), wait,
                names.get(t.assignee_id, "") if t.assignee_id else "", local_time(t.closed_at, zone), t.status,
                t.description]

    header = ["handoff_id", "conversation_id", "channel", "customer", f"queued_at ({zone.key})",
              f"first_response_at ({zone.key})", "wait_seconds", "assignee", f"closed_at ({zone.key})", "status", "reason"]
    return _export(db, principal, "handoffs", days, header, (row(t) for t in tickets))
