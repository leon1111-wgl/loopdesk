from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import prompt_version_name
from ..db import get_db
from ..models import Approval, Run, Span, User
from ..security import Principal, require_staff
from .common import get_owned, iso

router = APIRouter(prefix="/api/runs", tags=["traces"])


def run_out(r: Run, user: User | None = None) -> dict:
    return {
        "id": r.id, "conversation_id": r.conversation_id, "status": r.status, "model": r.model,
        "route_tier": r.route_tier, "input_text": r.input_text, "output_text": r.output_text, "steps": r.steps,
        "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens, "cost": r.cost,
        "latency_ms": r.latency_ms, "evidence_coverage": r.evidence_coverage, "verdict": r.verdict,
        "is_eval": r.is_eval, "error": r.error, "created_at": iso(r.created_at), "finished_at": iso(r.finished_at),
        "user": {"id": user.id, "name": user.name, "role": user.role} if user else None,
    }


@router.get("")
def list_runs(status: str | None = None, include_eval: bool = False, limit: int = 50, offset: int = 0,
              principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    query = select(Run, User).join(User, User.id == Run.user_id).where(Run.tenant_id == principal.tenant.id)
    if status:
        query = query.where(Run.status == status)
    if not include_eval:
        query = query.where(Run.is_eval.is_(False))
    rows = db.execute(query.order_by(Run.created_at.desc()).offset(offset).limit(min(limit, 200))).all()
    return {"items": [run_out(r, u) for r, u in rows]}


@router.get("/{run_id}")
def get_run(run_id: str, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    run = get_owned(db, Run, run_id, principal.tenant.id)
    spans = db.execute(select(Span).where(Span.run_id == run.id).order_by(Span.id)).scalars()
    approvals = db.execute(select(Approval).where(Approval.run_id == run.id)).scalars()
    return {
        **run_out(run, db.get(User, run.user_id)),
        "evidence": (run.state or {}).get("evidence", []),
        "request_id": (run.state or {}).get("request_id"),
        "prompt_version": prompt_version_name((run.state or {}).get("prompt_version")),
        "spans": [
            {"id": s.id, "kind": s.kind, "name": s.name, "input": s.input, "output": s.output, "status": s.status,
             "latency_ms": s.latency_ms, "prompt_tokens": s.prompt_tokens, "completion_tokens": s.completion_tokens,
             "started_at": iso(s.started_at)}
            for s in spans
        ],
        "approvals": [{"id": a.id, "status": a.status, "tool_name": a.tool_name, "reason": a.reason} for a in approvals],
    }
