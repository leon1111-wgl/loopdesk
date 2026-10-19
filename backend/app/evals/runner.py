"""Offline regression suite.

Each case runs through the real agent loop in dry-run mode (tools validate and answer,
but write nothing), then deterministic checks score the trajectory, not just the text:
which tools were called, whether a human approval was requested, whether the expected
document was retrieved, whether the answer passed the evidence check.
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ..agent.outcome import is_refusal  # one definition for evals and the dashboard
from ..agent.runtime import RunOptions, collect
from ..db import Base
from ..models import Chunk, Conversation, Document, EvalCase, EvalResult, EvalRun, Run, Span, Tenant, User
from ..seed import fixture_orders, fixture_refunds



def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def _resolve_user(db: Session, tenant: Tenant, case: EvalCase) -> User:
    if case.as_user:
        user = db.execute(select(User).where(User.email == case.as_user, User.tenant_id == tenant.id)).scalar_one_or_none()
        if user:
            return user
    user = db.execute(
        select(User).where(User.tenant_id == tenant.id, User.role == case.role).order_by(User.id)
    ).scalars().first()
    if user is None:
        raise ValueError(f"租户 {tenant.slug} 没有角色为 {case.role} 的用户")
    return user


def _clone(obj):
    return type(obj)(**{c.key: getattr(obj, c.key) for c in obj.__table__.columns})


def build_sandbox(db: Session, tenant: Tenant) -> Session:
    """In-memory copy of everything an eval needs.

    Knowledge base and tenant configuration come from the live database (that is what we
    want to evaluate); business data comes from fixed fixtures (so a refund someone made
    in the demo cannot turn a passing case red).
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sandbox = sessionmaker(bind=engine, expire_on_commit=False)()
    sandbox.add(_clone(tenant))
    users = db.execute(select(User).where(User.tenant_id == tenant.id)).scalars().all()
    sandbox.add_all(_clone(u) for u in users)
    sandbox.add_all(_clone(d) for d in db.execute(select(Document).where(Document.tenant_id == tenant.id)).scalars())
    sandbox.add_all(_clone(c) for c in db.execute(select(Chunk).where(Chunk.tenant_id == tenant.id)).scalars())
    sandbox.flush()
    sandbox.add_all(fixture_orders(tenant.slug, {u.email: sandbox.get(User, u.id) for u in users}))
    sandbox.flush()
    sandbox.add_all(fixture_refunds(tenant.slug, tenant.id))
    sandbox.commit()
    return sandbox


def _copy_trace(sandbox: Session, db: Session, run_id: str | None) -> None:
    """Keep eval traces inspectable in the main trace view."""
    run = sandbox.get(Run, run_id) if run_id else None
    if run is None:
        return
    copy = _clone(run)
    copy.conversation_id = None  # multi-turn cases use a sandbox-only conversation
    db.add(copy)
    db.flush()
    for span in sandbox.execute(select(Span).where(Span.run_id == run_id).order_by(Span.id)).scalars():
        data = {c.key: getattr(span, c.key) for c in Span.__table__.columns if c.key != "id"}
        db.add(Span(**data))


def retrieval_rank(case: EvalCase, summary: dict) -> int | None:
    """1-based rank of the first chunk from the expected document in the first search,
    0 when it was not retrieved, None when the case does not name an expected document."""
    if not case.expected_doc:
        return None
    for event in summary.get("tool_results", []):
        if event["name"] == "search_knowledge":
            for rank, hit in enumerate(event["output"].get("results") or [], start=1):
                if hit["doc"] == case.expected_doc:
                    return rank
            return 0
    return 0


def score_case(case: EvalCase, summary: dict) -> tuple[bool, dict, str, list[str]]:
    answer = (summary.get("final") or {}).get("text", "")
    tools = summary.get("tool_calls", [])
    retrieved_docs = {
        hit["doc"]
        for event in summary.get("tool_results", [])
        if event["name"] == "search_knowledge"
        for hit in (event["output"].get("results") or [])
    }
    checks: dict[str, bool] = {}
    if case.expected_tools:
        checks["tools"] = all(t in tools for t in case.expected_tools)
    if case.forbidden_tools:
        checks["forbidden_tools"] = not any(t in tools for t in case.forbidden_tools)
    if case.must_include:
        checks["must_include"] = all(s in answer for s in case.must_include)
    if case.must_not_include:
        checks["must_not_include"] = not any(s in answer for s in case.must_not_include)
    if case.expect_refusal is not None:
        checks["refusal"] = is_refusal(answer) == case.expect_refusal
    if case.expect_approval is not None:
        checks["approval"] = bool(summary.get("approval")) == case.expect_approval
    if case.expected_doc:
        checks["retrieval_hit"] = case.expected_doc in retrieved_docs
    verification = summary.get("verification")
    if verification:
        checks["evidence"] = verification["verdict"] == "pass"
    return all(checks.values()), checks, answer, tools


def run_suite(db: Session, services, tenant: Tenant, suite: str = "default", triggered_by: str = "manual",
              prompt_version: str | None = None) -> EvalRun:
    prompt_version = prompt_version or services.settings.prompt_version
    cases = db.execute(
        select(EvalCase).where(EvalCase.tenant_id == tenant.id, EvalCase.suite == suite, EvalCase.status == "active")
        .order_by(EvalCase.id)
    ).scalars().all()
    eval_run = EvalRun(tenant_id=tenant.id, suite=suite, total=len(cases), triggered_by=triggered_by,
                       model=f"{services.settings.llm_provider}:{services.settings.llm_model_strong}")
    db.add(eval_run)
    db.commit()
    sandbox = build_sandbox(db, tenant)
    sandbox_tenant = sandbox.get(Tenant, tenant.id)

    latencies: list[int] = []
    tokens = 0
    cost = 0.0
    check_totals: dict[str, list[bool]] = defaultdict(list)
    tag_totals: dict[str, list[bool]] = defaultdict(list)
    coverages: list[float] = []
    ranks: list[int] = []
    passed_count = 0
    for case in cases:
        user = _resolve_user(sandbox, sandbox_tenant, case)
        started = time.perf_counter()
        options = RunOptions(dry_run=True, prompt_version=prompt_version)
        conversation = None
        if case.history:  # multi-turn case: replay the earlier user turns in a sandbox conversation
            conversation = Conversation(tenant_id=sandbox_tenant.id, user_id=user.id, channel="eval")
            sandbox.add(conversation)
            sandbox.commit()
            for earlier in case.history:
                collect(services.runtime(sandbox).start(tenant=sandbox_tenant, user=user, text=earlier,
                                                        conversation=conversation, options=options))
        summary = collect(services.runtime(sandbox).start(tenant=sandbox_tenant, user=user, text=case.question,
                                                          conversation=conversation, options=options))
        latency = int((time.perf_counter() - started) * 1000)
        passed, checks, answer, tools = score_case(case, summary)
        rank = retrieval_rank(case, summary)
        if rank is not None:
            ranks.append(rank)
        run_id = (summary.get("done") or {}).get("run_id")
        run = sandbox.get(Run, run_id) if run_id else None
        if run is not None:  # the run record, not the event: customers' events carry no usage data
            tokens += run.prompt_tokens + run.completion_tokens
            cost += run.cost
        latencies.append(latency)
        if summary.get("verification"):
            coverages.append(summary["verification"]["coverage"])
        for name, ok in checks.items():
            check_totals[name].append(ok)
        for tag in case.tags or []:
            tag_totals[tag].append(passed)
        passed_count += int(passed)
        db.add(EvalResult(eval_run_id=eval_run.id, case_id=case.id, case_key=case.key, passed=passed,
                          checks=checks, answer=answer, tools_called=tools, latency_ms=latency,
                          run_id=run_id))
        _copy_trace(sandbox, db, run_id)
        db.commit()
    sandbox.close()

    def rate(values: list[bool]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    previous = db.execute(
        select(EvalRun).where(EvalRun.tenant_id == tenant.id, EvalRun.suite == suite,
                              EvalRun.status == "finished", EvalRun.id < eval_run.id)
        .order_by(EvalRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    eval_run.passed = passed_count
    eval_run.metrics = {
        "pass_rate": rate([True] * passed_count + [False] * (len(cases) - passed_count)) or 0.0,
        "checks": {name: rate(values) for name, values in check_totals.items()},
        "by_tag": {tag: rate(values) for tag, values in sorted(tag_totals.items())},
        "avg_evidence_coverage": round(statistics.mean(coverages), 4) if coverages else None,
        # Ranking quality, reported but not gated: Hit@1 and MRR of the expected document.
        "retrieval_hit_at_1": round(sum(1 for r in ranks if r == 1) / len(ranks), 4) if ranks else None,
        "retrieval_mrr": round(sum(1 / r for r in ranks if r) / len(ranks), 4) if ranks else None,
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "total_tokens": tokens,
        "estimated_cost": round(cost, 6),
        "prompt_version": prompt_version,
        "diff": diff_runs(db, previous, eval_run) if previous else None,
    }
    eval_run.status = "finished"
    eval_run.finished_at = datetime.now(timezone.utc)
    db.commit()
    return eval_run


def diff_runs(db: Session, before: EvalRun, after: EvalRun) -> dict:
    def results(run: EvalRun) -> dict[str, bool]:
        rows = db.execute(select(EvalResult.case_key, EvalResult.passed).where(EvalResult.eval_run_id == run.id)).all()
        return {key: passed for key, passed in rows}

    old, new = results(before), results(after)
    return {
        "against_run": before.id,
        "regressions": sorted(k for k in new if k in old and old[k] and not new[k]),
        "fixes": sorted(k for k in new if k in old and not old[k] and new[k]),
        "new_cases": sorted(k for k in new if k not in old),
        "removed_cases": sorted(k for k in old if k not in new),
    }
