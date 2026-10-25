"""Run the regression suite from the command line (used as the CI gate).

    python -m app.evals.cli --tenant leon --gate 0.9            # against LOOPDESK_DATABASE_URL
    python -m app.evals.cli --fresh --gate 0.9 --report out.json  # throwaway DB seeded from scratch
    python -m app.evals.cli --fresh --compare 1.00,2.00           # offline A/B of two prompt versions
    python -m app.evals.cli --fresh --embedding-down              # the embedding service fails after seeding
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from .scratch import scratch_database_url


class _DownEmbedder:
    """An embedding service that answers every call with HTTP 500."""

    def __init__(self, dim: int):
        self.dim = dim

    def embed(self, texts):
        from ..llm.base import LLMError

        raise LLMError("embedding HTTP 500: simulated outage (--embedding-down)")

    def embed_query(self, text):
        return self.embed([text])[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoopDesk offline eval")
    parser.add_argument("--tenant", default="leon")
    parser.add_argument("--suite", default="default")
    parser.add_argument("--gate", type=float, default=0.0, help="fail (exit 1) when pass rate is below this")
    parser.add_argument("--fresh", action="store_true", help="use a temporary database seeded with demo data")
    parser.add_argument("--report", help="write the metrics and per-case results as JSON")
    parser.add_argument("--prompt-version", help="evaluate this prompt version instead of the configured default")
    parser.add_argument("--compare", help="baseline,candidate prompt versions; fails on any regression")
    parser.add_argument("--embedding-down", action="store_true",
                        help="every embedding call fails after seeding (N-078: how the suite does in an outage)")
    args = parser.parse_args(argv)

    from ..config import get_settings, prompt_version_name
    from ..db import create_all, current_revision, init_engine, new_session, prepare_database, schema_status
    from ..models import EvalResult, Tenant
    from ..seed import seed_demo
    from ..services import Services, set_services
    from .runner import run_suite

    settings = get_settings()
    if args.fresh:
        settings.database_url = scratch_database_url("eval")
    init_engine(settings.database_url)
    if args.fresh:
        create_all()  # a throwaway database
    else:  # the configured database: the app's schema rules (a release job may own the migrations)
        prepare_database(settings.database_url, settings.auto_migrate, settings.external_migrations)
        status = schema_status(current_revision()) if settings.external_migrations else "ok"
        if status != "ok":
            print(f"the database schema is not ready ({status}); run the migration job, or use --fresh")
            return 2
    services = Services(settings)
    set_services(services)
    with new_session() as db:
        if args.fresh or settings.seed_demo:  # never the demo tenants and their public password in production
            seed_demo(db, services.embedder)
        if args.embedding_down:
            services.embedder = _DownEmbedder(services.embedder.dim)
        tenant = db.execute(select(Tenant).where(Tenant.slug == args.tenant)).scalar_one_or_none()
        if tenant is None:
            print(f"no tenant {args.tenant!r} in this database; use --fresh for the demo tenants")
            return 2
        if args.compare:
            return compare(db, services, tenant, args.suite, args.compare.split(","), args.gate)
        run = run_suite(db, services, tenant, args.suite, triggered_by="cli",
                        prompt_version=prompt_version_name(args.prompt_version))
        results = db.execute(select(EvalResult).where(EvalResult.eval_run_id == run.id)).scalars().all()

        print(f"\nLoopDesk eval · tenant={args.tenant} suite={args.suite} model={run.model}")
        print("-" * 78)
        for r in results:
            failed = [k for k, ok in r.checks.items() if not ok]
            mark = "PASS" if r.passed else "FAIL"
            print(f"{mark}  {r.case_key:<28} {r.latency_ms:>5}ms  {'' if r.passed else 'failed: ' + ', '.join(failed)}")
        m = run.metrics
        print("-" * 78)
        print(f"pass rate {m['pass_rate']:.1%} ({run.passed}/{run.total})  "
              f"p50 {m['latency_p50_ms']}ms  p95 {m['latency_p95_ms']}ms  tokens {m['total_tokens']}")
        print("checks:", json.dumps(m["checks"], ensure_ascii=False))
        print(f"retrieval: hit@1 {m.get('retrieval_hit_at_1')}  mrr {m.get('retrieval_mrr')}")
        if m.get("diff"):
            print("diff vs previous run:", json.dumps(m["diff"], ensure_ascii=False))
        if args.report:
            repairs = _repairs(db, [r.run_id for r in results if r.run_id])
            m["repairs"] = dict(Counter(a.split(" ")[0] for actions in repairs.values() for a in actions))
            Path(args.report).write_text(json.dumps({
                "metrics": m, "model": run.model, "total": run.total, "passed": run.passed,
                "cases": [{"key": r.case_key, "passed": r.passed, "checks": r.checks, "answer": r.answer,
                           "tools": r.tools_called, "latency_ms": r.latency_ms,
                           "repairs": repairs.get(r.run_id, [])} for r in results],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        if m["pass_rate"] < args.gate:
            print(f"\nGATE FAILED: pass rate {m['pass_rate']:.1%} < {args.gate:.0%}")
            return 1
    return 0


def _repairs(db, run_ids: list[str]) -> dict[str, list[str]]:
    """What the tiered answer check did on each run (LOOPDESK_VERIFIER=tiered), in order: a re-ask shows
    as two checks ("reask", then what the second answer got)."""
    from ..models import Span

    out: dict[str, list[str]] = {}
    if not run_ids:
        return out
    rows = db.execute(select(Span.run_id, Span.output).where(Span.run_id.in_(run_ids), Span.name == "evidence_check")
                      .order_by(Span.id)).all()
    for run_id, output in rows:
        if (output or {}).get("repair"):
            failed = [f"{c.get('reason') or c.get('contradicts')}: {c['text']}" for c in output.get("sentences") or []
                      if c.get("factual") and not c.get("supported")]
            out.setdefault(run_id, []).append(output["repair"] if output["repair"] == "pass"
                                              else f"{output['repair']} ({' | '.join(failed)[:300]})")
    return out


def compare(db, services, tenant, suite: str, versions: list[str], gate: float) -> int:
    """Run the same suite under two prompt versions and report flips and cost."""
    from ..agent.prompts import PROMPT_VERSIONS
    from ..config import prompt_version_name
    from ..models import EvalResult
    from .runner import run_suite

    baseline, candidate = (prompt_version_name(v) for v in versions[:2])
    unknown = [v for v in (baseline, candidate) if v not in PROMPT_VERSIONS]
    if unknown:
        print(f"unknown prompt version(s): {', '.join(unknown)}; known: {', '.join(PROMPT_VERSIONS)}")
        return 2
    runs = {v: run_suite(db, services, tenant, suite, triggered_by=f"ab:{v}", prompt_version=v) for v in (baseline, candidate)}
    results = {
        v: {r.case_key: r.passed for r in db.execute(select(EvalResult).where(EvalResult.eval_run_id == run.id)).scalars()}
        for v, run in runs.items()
    }
    regressions = sorted(k for k, ok in results[candidate].items() if results[baseline].get(k) and not ok)
    fixes = sorted(k for k, ok in results[candidate].items() if ok and results[baseline].get(k) is False)
    print(f"\nPrompt A/B · tenant={tenant.slug} suite={suite}")
    print(f"{'version':<10}{'pass rate':>12}{'tokens':>10}{'p50':>8}{'p95':>8}")
    for v, run in runs.items():
        m = run.metrics
        print(f"{v:<10}{m['pass_rate']:>12.1%}{m['total_tokens']:>10}{m['latency_p50_ms']:>6}ms{m['latency_p95_ms']:>6}ms")
    base_tokens = runs[baseline].metrics["total_tokens"] or 1
    delta = (runs[candidate].metrics["total_tokens"] - base_tokens) / base_tokens
    print(f"token change {candidate} vs {baseline}: {delta:+.1%}")
    print(f"regressions: {regressions or 'none'}; fixes: {fixes or 'none'}")
    if regressions or runs[candidate].metrics["pass_rate"] < gate:
        print("\nA/B GATE FAILED: the candidate must not regress any case")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
