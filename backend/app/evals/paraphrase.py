"""Paraphrase recall (review round 4, N-029): do customers' own wordings of a policy reach it?

    python -m app.evals.paraphrase --report paraphrase.json

The questions in ``evals/paraphrase.json`` are held out: written before any measurement and never
used to tune the synonym table, the stop words or the thresholds. For each question:

- recall@4: the section that answers it is among the top 4 retrieved, ignoring the gate;
- answered: it passes the relevance gate with that section among the hits the agent would see
  (after the same relative cut-off as the search tool);
- refused: nothing passes the gate, so the agent would refuse;
- wrong: something passes, but not that section.

A measurement, not a CI gate. Read it next to gate_probe's out-of-scope set: a change that answers
more paraphrases must not let more out-of-scope questions through.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select

from .scratch import scratch_database_url

SET_FILE = Path(__file__).resolve().parents[2] / "evals" / "paraphrase.json"


def measure(settings=None) -> dict:
    from ..config import get_settings
    from ..db import create_all, init_engine, new_session
    from ..models import Tenant
    from ..rag.retrieval import gate_options
    from ..seed import seed_demo
    from ..services import Services

    settings = settings or get_settings()
    settings.database_url = scratch_database_url("paraphrase")
    init_engine(settings.database_url)
    create_all()
    services = Services(settings)
    with new_session() as db:
        seed_demo(db, services.embedder)
        tenant = db.execute(select(Tenant).where(Tenant.slug == "leon")).scalar_one()
        return measure_on(db, tenant.id, services.embedder, services.reranker, gate_options(settings))


def measure_on(db, tenant_id: int, embedder, reranker, gate: dict, questions: list[dict] | None = None,
               match=None) -> dict:
    """The set's outcome on one seeded tenant. ``questions`` / ``match`` let the blind probe set reuse
    it: [{"q", "section", "also"?}], and whether a hit's heading is the labelled section."""
    from ..rag.retrieval import hybrid_search

    questions = questions if questions is not None else json.loads(SET_FILE.read_text(encoding="utf-8"))["questions"]
    match = match or (lambda heading, item: item["section"] in heading)
    ungated = {**gate, "min_relevance": 0.0, "min_cosine": 0.0, "min_rerank": None, "rerank_floor": None}
    counts = {"recall_at_4": 0, "answered": 0, "refused": 0, "wrong": 0}
    misses = []
    for item in questions:
        question = item["q"]
        top, _ = hybrid_search(db, tenant_id, question, embedder, {"public"}, top_k=4, reranker=reranker, **ungated)
        gated, _ = hybrid_search(db, tenant_id, question, embedder, {"public"}, top_k=4, reranker=reranker, **gate)
        if gated:  # the search tool's relative cut-off
            floor = 0.5 * max(h.relevance for h in gated)
            gated = [h for h in gated if h.relevance >= floor]
        in_top = any(match(h.heading, item) for h in top)
        counts["recall_at_4"] += in_top
        if not gated:
            outcome = "refused"
        elif any(match(h.heading, item) for h in gated):
            outcome = "answered"
        else:
            outcome = "wrong"
        counts[outcome] += 1
        if outcome != "answered":
            best = max(top, key=lambda h: h.coverage, default=None)
            misses.append({"question": question, "expected": item["section"], "outcome": outcome,
                           "in_top_4": in_top, "best_coverage": round(best.coverage, 3) if best else 0.0,
                           "best_heading": best.heading if best else ""})
    total = len(questions)
    return {"total": total, **{k: f"{v}/{total}" for k, v in counts.items()}, "misses": misses}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoopDesk paraphrase recall")
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    report = measure()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
