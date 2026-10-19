from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Approval, EvalRun, Feedback, Run, Span
from ..security import Principal, require_staff
from .common import as_utc

router = APIRouter(tags=["metrics"])


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def _by_prompt_version(runs) -> dict:
    """Canary read-out: compare outcomes of each prompt version on live traffic."""
    groups: dict[str, list] = {}
    for r in runs:
        groups.setdefault((r.state or {}).get("prompt_version") or "v1", []).append(r)
    out = {}
    for version, items in sorted(groups.items()):
        coverages = [r.evidence_coverage for r in items if r.evidence_coverage is not None]
        out[version] = {
            "runs": len(items),
            "handoff_rate": round(sum(1 for r in items if r.status == "handoff") / len(items), 4),
            "blocked": sum(1 for r in items if r.verdict == "blocked"),
            "avg_evidence_coverage": round(sum(coverages) / len(coverages), 4) if coverages else None,
            "avg_tokens": round(sum(r.prompt_tokens + r.completion_tokens for r in items) / len(items), 1),
        }
    return out


@router.get("/api/metrics/overview")
def overview(days: int = 7, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 90)))
    tenant_id = principal.tenant.id
    runs = db.execute(
        select(Run).where(Run.tenant_id == tenant_id, Run.is_eval.is_(False), Run.created_at >= since)
    ).scalars().all()
    run_ids = [r.id for r in runs]
    tool_counts = Counter(
        db.execute(select(Span.name).where(Span.run_id.in_(run_ids), Span.kind == "tool")).scalars()
    ) if run_ids else Counter()
    statuses = Counter(r.status for r in runs)
    verdicts = Counter(r.verdict for r in runs if r.verdict)
    latencies = [r.latency_ms for r in runs if r.status != "running"]
    coverages = [r.evidence_coverage for r in runs if r.evidence_coverage is not None]
    feedback = Counter(db.execute(
        select(Feedback.rating).where(Feedback.tenant_id == tenant_id, Feedback.created_at >= since)
    ).scalars())
    window = max(1, min(days, 90))
    today = datetime.now(timezone.utc).date()
    daily: dict[str, dict[str, int]] = {
        (today - timedelta(days=offset)).strftime("%m-%d"): {"runs": 0, "handoff": 0}
        for offset in range(window - 1, -1, -1)
    }
    for r in runs:
        day = as_utc(r.created_at).strftime("%m-%d")
        bucket = daily.setdefault(day, {"runs": 0, "handoff": 0})
        bucket["runs"] += 1
        bucket["handoff"] += int(r.status == "handoff")
    last_eval = db.execute(
        select(EvalRun).where(EvalRun.tenant_id == tenant_id, EvalRun.status == "finished").order_by(EvalRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    pending = db.execute(
        select(func.count(Approval.id)).where(Approval.tenant_id == tenant_id, Approval.status == "pending")
    ).scalar_one()
    total = len(runs)
    return {
        "window_days": days,
        "runs": total,
        "status": dict(statuses),
        "resolution_rate": round(statuses.get("completed", 0) / total, 4) if total else None,
        "handoff_rate": round(statuses.get("handoff", 0) / total, 4) if total else None,
        "pending_approvals": pending,
        "latency_p50_ms": _pct(latencies, 0.5),
        "latency_p95_ms": _pct(latencies, 0.95),
        "tokens": sum(r.prompt_tokens + r.completion_tokens for r in runs),
        "cost": round(sum(r.cost for r in runs), 6),
        "avg_evidence_coverage": round(sum(coverages) / len(coverages), 4) if coverages else None,
        "verdicts": dict(verdicts),
        "tools": dict(tool_counts),
        "route_tiers": dict(Counter(r.route_tier for r in runs if r.route_tier)),
        "cache_hits": sum(1 for r in runs if r.route_tier == "cache"),
        "prompt_versions": _by_prompt_version(runs),
        "cache_hit_rate": round(sum(1 for r in runs if r.route_tier == "cache") / total, 4) if total else None,
        "feedback": dict(feedback),
        "daily": [{"day": k, **v} for k, v in daily.items()],
        "last_eval": {"id": last_eval.id, "pass_rate": last_eval.metrics.get("pass_rate"), "total": last_eval.total}
        if last_eval else None,
    }


@router.get("/metrics", response_class=PlainTextResponse)
def prometheus(db: Session = Depends(get_db)) -> str:
    """Prometheus exposition: aggregate counters only, no tenant data."""
    rows = db.execute(
        select(Run.status, func.count(Run.id), func.sum(Run.prompt_tokens), func.sum(Run.completion_tokens))
        .where(Run.is_eval.is_(False)).group_by(Run.status)
    ).all()
    lines = ["# HELP loopdesk_runs_total Agent runs by final status", "# TYPE loopdesk_runs_total counter"]
    lines += [f'loopdesk_runs_total{{status="{s}"}} {c}' for s, c, _, _ in rows]
    lines += ["# HELP loopdesk_tokens_total Tokens consumed", "# TYPE loopdesk_tokens_total counter"]
    lines.append(f'loopdesk_tokens_total{{type="prompt"}} {sum(p or 0 for _, _, p, _ in rows)}')
    lines.append(f'loopdesk_tokens_total{{type="completion"}} {sum(c or 0 for _, _, _, c in rows)}')
    return "\n".join(lines) + "\n"
