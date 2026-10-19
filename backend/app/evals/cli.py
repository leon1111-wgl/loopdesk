"""Run the regression suite from the command line (used as the CI gate).

    python -m app.evals.cli --tenant leon --gate 0.9            # against LOOPDESK_DATABASE_URL
    python -m app.evals.cli --fresh --gate 0.9 --report out.json  # throwaway DB seeded from scratch
    python -m app.evals.cli --fresh --compare v1,v2               # offline A/B of two prompt versions
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from sqlalchemy import select


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoopDesk offline eval")
    parser.add_argument("--tenant", default="leon")
    parser.add_argument("--suite", default="default")
    parser.add_argument("--gate", type=float, default=0.0, help="fail (exit 1) when pass rate is below this")
    parser.add_argument("--fresh", action="store_true", help="use a temporary database seeded with demo data")
    parser.add_argument("--report", help="write the metrics and per-case results as JSON")
    parser.add_argument("--prompt-version", help="evaluate this prompt version instead of the configured default")
    parser.add_argument("--compare", help="baseline,candidate prompt versions; fails on any regression")
    args = parser.parse_args(argv)

    from ..config import get_settings
    from ..db import create_all, init_engine, new_session
    from ..models import EvalResult, Tenant
    from ..seed import seed_demo
    from ..services import Services, set_services
    from .runner import run_suite

    settings = get_settings()
    if args.fresh:
        settings.database_url = f"sqlite:///{Path(tempfile.mkdtemp()) / 'eval.db'}"
    init_engine(settings.database_url)
    create_all()
    services = Services(settings)
    set_services(services)
    with new_session() as db:
        seed_demo(db, services.embedder)
        tenant = db.execute(select(Tenant).where(Tenant.slug == args.tenant)).scalar_one()
        if args.compare:
            return compare(db, services, tenant, args.suite, args.compare.split(","), args.gate)
        run = run_suite(db, services, tenant, args.suite, triggered_by="cli", prompt_version=args.prompt_version)
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
        if m.get("diff"):
            print("diff vs previous run:", json.dumps(m["diff"], ensure_ascii=False))
        if args.report:
            Path(args.report).write_text(json.dumps({
                "metrics": m, "model": run.model, "total": run.total, "passed": run.passed,
                "cases": [{"key": r.case_key, "passed": r.passed, "checks": r.checks, "answer": r.answer,
                           "tools": r.tools_called, "latency_ms": r.latency_ms} for r in results],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        if m["pass_rate"] < args.gate:
            print(f"\nGATE FAILED: pass rate {m['pass_rate']:.1%} < {args.gate:.0%}")
            return 1
    return 0


def compare(db, services, tenant, suite: str, versions: list[str], gate: float) -> int:
    """Run the same suite under two prompt versions and report flips and cost."""
    from ..agent.prompts import PROMPT_VERSIONS
    from ..models import EvalResult
    from .runner import run_suite

    baseline, candidate = (v.strip() for v in versions[:2])
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
