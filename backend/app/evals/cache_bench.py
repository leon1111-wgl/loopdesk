"""Measure the answer cache on the FAQ part of the eval set.

Each knowledge question (tag "rag" or "refusal", asked as a customer) goes through the
real runtime twice, each time in a new conversation. The report compares the two passes.

    python -m app.evals.cache_bench --report cache.json

Token counts come from the offline model's estimates, so read them as relative savings
of the pipeline, not as a real model bill.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
from pathlib import Path

from sqlalchemy import select


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoopDesk answer-cache benchmark")
    parser.add_argument("--tenant", default="leon")
    parser.add_argument("--report")
    args = parser.parse_args(argv)

    from ..agent.runtime import collect
    from ..config import get_settings
    from ..db import create_all, init_engine, new_session
    from ..models import Conversation, EvalCase, Run, Tenant, User
    from ..seed import seed_demo
    from ..services import Services

    settings = get_settings()
    settings.database_url = f"sqlite:///{Path(tempfile.mkdtemp()) / 'cache.db'}"
    init_engine(settings.database_url)
    create_all()
    services = Services(settings)
    with new_session() as db:
        seed_demo(db, services.embedder)
        tenant = db.execute(select(Tenant).where(Tenant.slug == args.tenant)).scalar_one()
        customer = db.execute(select(User).where(User.tenant_id == tenant.id, User.role == "customer")).scalars().first()
        cases = [c for c in db.execute(select(EvalCase).where(EvalCase.tenant_id == tenant.id)).scalars()
                 if c.role == "customer" and {"rag", "refusal"} & set(c.tags or [])]
        passes: list[list[dict]] = [[], []]
        for round_ in range(2):
            for case in cases:
                conversation = Conversation(tenant_id=tenant.id, user_id=customer.id)
                db.add(conversation)
                db.commit()
                summary = collect(services.runtime(db).start(tenant=tenant, user=customer, text=case.question,
                                                             conversation=conversation))
                done = summary["done"]
                run = db.get(Run, done["run_id"])  # customers' events carry no usage data
                passes[round_].append({
                    "key": case.key, "tier": run.route_tier, "latency_ms": done["latency_ms"],
                    "tokens": run.prompt_tokens + run.completion_tokens,
                    "answer": summary["final"]["text"],
                })

    first, second = passes
    hits = sum(1 for r in second if r["tier"] == "cache")
    tokens_first = sum(r["tokens"] for r in first)
    tokens_second = sum(r["tokens"] for r in second)
    report = {
        "questions": len(first),
        "second_pass_hit_rate": round(hits / len(second), 4) if second else 0.0,
        "answers_identical": all(a["answer"] == b["answer"] for a, b in zip(first, second)),
        "tokens_first_pass": tokens_first,
        "tokens_second_pass": tokens_second,
        "latency_p50_first_ms": statistics.median(r["latency_ms"] for r in first) if first else 0,
        "latency_p50_second_ms": statistics.median(r["latency_ms"] for r in second) if second else 0,
        "not_cached": [r["key"] for r in second if r["tier"] != "cache"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        Path(args.report).write_text(json.dumps({"report": report, "passes": passes}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
