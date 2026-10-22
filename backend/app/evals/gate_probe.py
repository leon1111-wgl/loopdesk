"""Probe the retrieval relevance gate: does it let in-scope questions through and keep
out-of-scope ones out?

    python -m app.evals.gate_probe --report gate.json
    python -m app.evals.gate_probe --split heldout                 # the held-out half only
    python -m app.evals.gate_probe --calibrate --split dev         # thresholds for the models in use

The question lists live in ``evals/gate_probe.json`` (customer scope, leon tenant). A
question "passes the gate" when hybrid search returns at least one chunk the gate lets through.
This is a measurement, not a CI gate: the misses it lists are the known limits of a lexical gate
(see docs/EVALUATION.md).

The file fixes a dev / held-out split (W5): semantic thresholds are calibrated on dev only, and the
held-out half is reported, never tuned on. ``calibrate`` sets them by margins rather than by
searching the dev questions, so a small dev set cannot be fitted:

- ``min_cosine`` / ``min_rerank`` (pass on the semantic score alone): just above the best score any
  dev out-of-scope question reaches, so none of them passes that way;
- ``rerank_floor`` (veto a coverage pass): just below the best rerank score of the weakest dev
  in-scope question among its coverage-passing chunks, so none of them is vetoed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select

from .scratch import scratch_database_url

PROBE_FILE = Path(__file__).resolve().parents[2] / "evals" / "gate_probe.json"
PROBE_TENANT = "leon"
SPLITS = ("dev", "heldout")
MARGIN = 0.02


def load_probe(tenant: str = PROBE_TENANT, split: str | None = None) -> dict | None:
    """{"in_scope": [...], "out_of_scope": [...]} for the tenant (None: it has no probe set), all of
    it or one half of the split."""
    if split is not None and split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}")
    if tenant != PROBE_TENANT:
        return None
    data = json.loads(PROBE_FILE.read_text(encoding="utf-8"))
    held = data.get("split", {}).get("heldout", {})
    out = {}
    for name in ("in_scope", "out_of_scope"):
        questions = data[name]
        if split == "heldout":
            questions = [q for q in questions if q in held.get(name, [])]
        elif split == "dev":
            questions = [q for q in questions if q not in held.get(name, [])]
        out[name] = questions
    return out


def _best(candidates: list[dict], key: str, where=lambda c: True) -> float | None:
    values = [c[key] for c in candidates if c.get(key) is not None and where(c)]
    return max(values) if values else None


def calibrate(inside: list[list[dict]], outside: list[list[dict]], min_relevance: float,
              margin: float = MARGIN) -> dict:
    """Thresholds from per-question candidates ({"coverage", "cosine", "rerank"}) of the dev split."""
    def above(values):
        values = [v for v in values if v is not None]
        return round(min(1.0, max(values) + margin), 4) if values else None

    out_cos = [_best(q, "cosine") or 0.0 for q in outside]
    min_cosine = above(out_cos)
    has_rerank = any(c.get("rerank") is not None for q in inside + outside for c in q)
    min_rerank = above([_best(q, "rerank") or 0.0 for q in outside]) if has_rerank else None
    floor = None
    if has_rerank:
        lexical = lambda c: c["coverage"] >= min_relevance  # noqa: E731
        weakest = [_best(q, "rerank", lexical) for q in inside if any(lexical(c) for c in q)]
        weakest = [w for w in weakest if w is not None]
        if weakest:
            floor = round(max(0.0, min(weakest) - margin), 4)
            floor = min(floor, min_rerank) if min_rerank is not None else floor

    def vetoed(q):  # passes on coverage, and every coverage-passing chunk is under the floor
        passing = [c for c in q if c["coverage"] >= min_relevance]
        return bool(passing) and floor is not None and all((c.get("rerank") or 0.0) < floor for c in passing)

    alone = sum(1 for q in inside if min_rerank is not None and (_best(q, "rerank") or 0.0) >= min_rerank)
    return {
        "split": "dev", "min_cosine": min_cosine, "min_rerank": min_rerank, "rerank_floor": floor,
        "dev": {
            "in_scope": len(inside), "out_of_scope": len(outside),
            "in_scope_passing_on_cosine_alone": f"{sum((_best(q, 'cosine') or 0.0) >= min_cosine for q in inside)}/{len(inside)}"
            if min_cosine is not None else None,
            "in_scope_passing_on_rerank_alone": f"{alone}/{len(inside)}" if min_rerank is not None else None,
            "out_of_scope_vetoed": sum(vetoed(q) for q in outside),
        },
    }


def candidates(db, tenant_id: int, question: str, embedder, reranker=None, rerank_candidates: int = 20,
               index_questions: bool = True) -> list[dict]:
    """Every candidate the search would weigh, with its scores and no gate applied."""
    from ..rag.retrieval import hybrid_search

    hits, _ = hybrid_search(db, tenant_id, question, embedder, {"public"}, top_k=max(rerank_candidates, 20),
                            min_relevance=0.0, min_cosine=0.0, reranker=reranker, min_rerank=None,
                            rerank_floor=None, rerank_candidates=rerank_candidates, index_questions=index_questions)
    return [{"coverage": h.coverage, "cosine": h.cosine, "rerank": h.rerank, "heading": h.heading} for h in hits]


def probe(db, tenant_id: int, questions: dict, embedder, reranker, gate: dict) -> dict:
    """In-scope questions that pass the gate, out-of-scope ones it keeps out, and the misses."""
    from ..rag.retrieval import hybrid_search

    report: dict = {}
    for name, want in (("in_scope", True), ("out_of_scope", False)):
        misses = []
        for question in questions[name]:
            hits, _ = hybrid_search(db, tenant_id, question, embedder, {"public"}, reranker=reranker, **gate)
            best = max(hits, key=lambda h: h.relevance, default=None)
            if bool(hits) != want:
                misses.append({"question": question, "relevance": round(best.relevance, 3) if best else 0.0,
                               "matched": best.heading if best else ""})
        total = len(questions[name])
        report[name] = {"correct": total - len(misses), "total": total, "misses": misses}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoopDesk relevance-gate probe")
    parser.add_argument("--tenant", default=PROBE_TENANT)
    parser.add_argument("--split", choices=SPLITS, help="only this half of the dev / held-out split")
    parser.add_argument("--report")
    parser.add_argument("--calibrate", action="store_true",
                        help="suggest LOOPDESK_MIN_COSINE / MIN_RERANK / RERANK_FLOOR for the models in use (use --split dev)")
    args = parser.parse_args(argv)

    from ..config import get_settings
    from ..db import create_all, init_engine, new_session
    from ..models import Tenant
    from ..rag.retrieval import gate_options
    from ..seed import seed_demo
    from ..services import Services

    settings = get_settings()
    settings.database_url = scratch_database_url("gate")
    init_engine(settings.database_url)
    create_all()
    services = Services(settings)
    questions = load_probe(args.tenant, args.split)
    if questions is None:
        parser.error(f"no probe set for tenant {args.tenant!r}")
    gate = gate_options(settings)
    report: dict = {"split": args.split or "all", "gate": gate}
    with new_session() as db:
        seed_demo(db, services.embedder)
        tenant = db.execute(select(Tenant).where(Tenant.slug == args.tenant)).scalar_one()
        report.update(probe(db, tenant.id, questions, services.embedder, services.reranker, gate))
        if args.calibrate:
            if args.split != "dev":
                print("note: calibrating on more than the dev split; the held-out numbers are then not held out")
            scores = {name: [candidates(db, tenant.id, q, services.embedder, services.reranker,
                                        settings.rerank_candidates, settings.index_questions) for q in questions[name]]
                      for name in ("in_scope", "out_of_scope")}
            result = calibrate(scores["in_scope"], scores["out_of_scope"], settings.min_relevance)
            result["split"] = args.split or "all"
            result["embedder"] = settings.embedding_provider + (f":{settings.embedding_model}" if settings.embedding_model else "")
            result["reranker"] = services.reranker.name if services.reranker else None
            report["calibration"] = result

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
