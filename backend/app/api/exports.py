"""CSV exports for operations teams (runs, feedback, approvals).

- Every export is recorded in the audit log (who, what, how many rows) before the file is sent.

- UTF-8 with BOM, so Excel opens Chinese text without mojibake.
- Cells starting with = + - @ (or tab / carriage return) are prefixed with a single
  quote: user-controlled text such as a customer's question must not execute as a
  spreadsheet formula ("CSV injection").
- Tenant-scoped like every other endpoint; text fields are the already-masked versions
  stored in the database, so exports do not re-expose PII.
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
from ..models import Approval, Feedback, Run, Span, User
from ..security import Principal, rate_limiter, require_staff
from .common import iso

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
    header = ["run_id", "created_at", "user", "role", "input", "output", "status", "route_tier", "model", "steps",
              "latency_ms", "prompt_tokens", "completion_tokens", "cost", "evidence_verdict", "evidence_coverage", "tools"]
    return _export(db, principal, "runs", days, header, (
        [r.id, iso(r.created_at), u.name, u.role, r.input_text, r.output_text, r.status, r.route_tier, r.model, r.steps,
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
    header = ["feedback_id", "created_at", "user", "rating", "question", "answer", "correction", "reason", "attribution", "run_id"]
    return _export(db, principal, "feedback", days, header, (
        [f.id, iso(f.created_at), u.name, f.rating, f.question, f.answer, f.correction, f.reason_category, f.attribution, f.run_id]
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
    header = ["approval_id", "created_at", "decided_at", "tool", "order_id", "amount", "reason", "status",
              "requested_by", "reviewer", "comment", "run_id"]
    return _export(db, principal, "approvals", days, header, (
        [a.id, iso(a.created_at), iso(a.decided_at), a.tool_name, (a.arguments or {}).get("order_id"),
         (a.arguments or {}).get("amount"), a.reason, a.status, names.get(a.requested_by),
         names.get(a.reviewer_id) if a.reviewer_id else "", a.comment, a.run_id]
        for a in approvals
    ))
