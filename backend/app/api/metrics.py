from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..admission import get_admission
from ..config import prompt_version_name
from ..db import get_db
from ..agent.outcome import is_refusal
from ..agent.prompts import FIRST_PROMPT_VERSION
from ..models import Approval, EvalRun, Feedback, KnowledgeConflict, Message, Run, Span, Ticket
from ..security import Principal, require_staff
from ..localtime import start_of_day, zone_of
from ..llm.errors import error_kind
from .common import as_utc
from .handoffs import first_reply, reply_times
from .tickets import open_ticket_stats

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
        # Runs from before versioning used the first prompt; older runs name it v1.
        version = prompt_version_name((r.state or {}).get("prompt_version")) or FIRST_PROMPT_VERSION
        groups.setdefault(version, []).append(r)
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


def _handoff_stats(db: Session, tenant_id: int, since: datetime) -> dict:
    """Contact-centre view of human takeover: who is waiting now, and how fast people answer."""
    now = datetime.now(timezone.utc)
    live = db.execute(select(Ticket).where(Ticket.tenant_id == tenant_id, Ticket.kind == "handoff",
                                           Ticket.status.in_(("open", "in_progress")))).scalars().all()
    waiting = [t for t in live if t.status == "open"]
    recent = db.execute(select(Ticket).where(Ticket.tenant_id == tenant_id, Ticket.kind == "handoff",
                                             Ticket.created_at >= since)).scalars().all()
    times = reply_times(db, {t.conversation_id for t in recent if t.conversation_id})
    first_response = [(reply - as_utc(t.created_at)).total_seconds()
                      for t in recent if (reply := first_reply(t, times)) is not None]
    return {
        "waiting": len(waiting),
        "in_progress": len(live) - len(waiting),
        "longest_wait_s": int(max(((now - as_utc(t.created_at)).total_seconds() for t in waiting), default=0)),
        "handoffs": len(recent),
        "avg_first_response_s": int(sum(first_response) / len(first_response)) if first_response else None,
    }


def _conflict_backlog(db: Session, tenant_id: int, run_ids: list[int]) -> dict:
    """Open knowledge conflicts send every question resting on both sides to a person (see
    rag/conflicts.disputed), so the backlog is an operational number: how many, how old, and
    how many questions it has handed over in the window."""
    opened = db.execute(select(KnowledgeConflict.created_at).where(
        KnowledgeConflict.tenant_id == tenant_id, KnowledgeConflict.status == "open")).scalars().all()
    now = datetime.now(timezone.utc)
    handed_over = db.execute(select(func.count(func.distinct(Span.run_id))).where(
        Span.run_id.in_(run_ids), Span.kind == "guardrail", Span.name == "knowledge_conflict")).scalar_one() \
        if run_ids else 0
    return {
        "open": len(opened),
        "oldest_open_s": int(max(((now - as_utc(t)).total_seconds() for t in opened), default=0)),
        "handoffs": handed_over,
    }


def _unsafe_answers(db: Session, run_ids: list) -> dict:
    """Answers the output check stopped (N-038), and the documents that say the same thing: what
    an administrator should read. One with no such document came from the model or from the
    customer's prompt, not from the knowledge base."""
    rows = db.execute(select(Span.output).where(
        Span.run_id.in_(run_ids), Span.kind == "guardrail", Span.name == "unsafe_answer")).scalars().all() \
        if run_ids else []
    documents: dict[int, dict] = {}
    for output in rows:
        for source in {s["document_id"]: s for s in (output or {}).get("sources", [])}.values():
            entry = documents.setdefault(source["document_id"],
                                         {"id": source["document_id"], "title": source["title"], "answers": 0})
            entry["answers"] += 1
    return {"answers": len(rows), "without_source": sum(1 for o in rows if not (o or {}).get("sources")),
            "documents": sorted(documents.values(), key=lambda d: -d["answers"])}


def _conversation_outcomes(db: Session, runs: list[Run], tenant_id: int, since: datetime) -> dict:
    """Rates per conversation, not per run: three questions in one conversation are one customer.

    Resolved means the conversation never went to a person, never got a refusal, drew no
    negative feedback or correction, and its last run completed. A refusal ("知识库中没有找到
    依据") is not a resolution even though the run itself completed normally."""
    groups: dict[object, list[Run]] = {}
    for r in sorted(runs, key=lambda r: as_utc(r.created_at)):
        groups.setdefault(r.conversation_id or f"run:{r.id}", []).append(r)
    unhappy = set(db.execute(
        select(Message.conversation_id).join(Feedback, Feedback.message_id == Message.id)
        .where(Feedback.tenant_id == tenant_id, Feedback.created_at >= since, Feedback.rating.in_(("down", "correction")))
    ).scalars())
    resolved = refused = handed = 0
    for key, items in groups.items():
        handed_off = any(r.status == "handoff" for r in items)
        was_refused = any(is_refusal(r.output_text) for r in items if r.status == "completed")
        handed += handed_off
        refused += was_refused
        # A run that hit an error is not self-service, even when the customer got the quoted policy
        # (N-031): otherwise an outage would raise the resolution rate.
        resolved += (not handed_off and not was_refused and key not in unhappy and items[-1].status == "completed"
                     and not any(r.error for r in items))
    n = len(groups)
    return {
        "conversations": n,
        "resolution_rate": round(resolved / n, 4) if n else None,
        "handoff_rate": round(handed / n, 4) if n else None,
        "refusal_rate": round(refused / n, 4) if n else None,
    }


def _failures(runs: list[Run], now: datetime) -> dict:
    """Runs that hit an error (N-037). Since N-031 a customer gets the quoted policy or a person when
    the model fails, so the run is not "failed" and only this shows the outage: how many, how many in
    the last 15 minutes (the alert), and why."""
    errors = sorted((r for r in runs if r.error), key=lambda r: as_utc(r.created_at))
    return {
        "runs": len(errors),
        "rate": round(len(errors) / len(runs), 4) if runs else None,
        "last_15m": sum(1 for r in errors if now - as_utc(r.created_at) <= timedelta(minutes=15)),
        "kinds": dict(Counter(error_kind(r.error) for r in errors)),
        "last_error": errors[-1].error[:200] if errors else None,
        "last_kind": error_kind(errors[-1].error) if errors else None,
    }


@router.get("/api/metrics/overview")
def overview(days: int = 7, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    """``days`` local calendar days, today included, in the tenant's time zone (N-024): the days
    were UTC dates, and a rolling 7 x 24 hours put part of an eighth day at the end of the chart."""
    window = max(1, min(days, 90))
    zone = zone_of(principal.tenant)
    first = datetime.now(zone).date() - timedelta(days=window - 1)
    since = start_of_day(first, zone)
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
    daily: dict[str, dict[str, int]] = {
        (first + timedelta(days=offset)).strftime("%m-%d"): {"runs": 0, "handoff": 0} for offset in range(window)
    }
    for r in runs:
        bucket = daily.setdefault(as_utc(r.created_at).astimezone(zone).strftime("%m-%d"), {"runs": 0, "handoff": 0})
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
        "window_days": window,
        "timezone": principal.tenant.timezone,
        "runs": total,
        "status": dict(statuses),
        # Per conversation; see _conversation_outcomes for the definitions.
        **_conversation_outcomes(db, runs, tenant_id, since),
        "pending_approvals": pending,
        "handoff_queue": _handoff_stats(db, tenant_id, since),
        "knowledge_conflicts": _conflict_backlog(db, tenant_id, run_ids),
        "unsafe_answers": _unsafe_answers(db, run_ids),
        "tickets": open_ticket_stats(db, principal.tenant),
        "failures": _failures(runs, datetime.now(timezone.utc)),
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
    # Runs that hit an error, whatever their status: with the fallback (N-031) a model outage ends in
    # quoted policy or a handoff, not "failed". Alert on its rate (docs/DEPLOYMENT.md).
    errors = db.execute(select(func.count(Run.id)).where(Run.is_eval.is_(False), Run.error.isnot(None))).scalar_one()
    lines += ["# HELP loopdesk_run_errors_total Agent runs that hit an error (model, timeout, crash)",
              "# TYPE loopdesk_run_errors_total counter", f"loopdesk_run_errors_total {errors}"]
    # Per process, unlike the totals above (which come from the shared database): scrape every pod.
    # The autoscaler follows in-flight runs, since a pod waiting on the model is busy at low CPU (N-158).
    admission = get_admission()
    lines += ["# HELP loopdesk_inflight_runs Agent runs this process is serving now",
              "# TYPE loopdesk_inflight_runs gauge", f"loopdesk_inflight_runs {admission.inflight}",
              "# HELP loopdesk_inflight_runs_limit Runs this process admits at once (LOOPDESK_MAX_INFLIGHT_RUNS)",
              "# TYPE loopdesk_inflight_runs_limit gauge", f"loopdesk_inflight_runs_limit {admission.limit}",
              "# HELP loopdesk_runs_rejected_total Chat requests turned away with 503 because the process was full",
              "# TYPE loopdesk_runs_rejected_total counter", f"loopdesk_runs_rejected_total {admission.rejected}"]
    return "\n".join(lines) + "\n"
