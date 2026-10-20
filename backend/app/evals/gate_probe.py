"""Probe the retrieval relevance gate: does it let in-scope questions through and keep
out-of-scope ones out?

    python -m app.evals.gate_probe --report gate.json
    python -m app.evals.gate_probe --calibrate   # suggest LOOPDESK_MIN_COSINE for the embedder in use

The question lists live in ``evals/gate_probe.json`` (customer scope, leon tenant). A
question "passes the gate" when hybrid search returns at least one chunk at or above
``min_relevance``. This is a measurement, not a CI gate: the misses it lists are the
known limits of a lexical gate (see docs/EVALUATION.md).
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from sqlalchemy import select

PROBE_FILE = Path(__file__).resolve().parents[2] / "evals" / "gate_probe.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoopDesk relevance-gate probe")
    parser.add_argument("--tenant", default="leon")
    parser.add_argument("--report")
    parser.add_argument("--calibrate", action="store_true",
                        help="report the best cosine each probe question reaches and suggest LOOPDESK_MIN_COSINE")
    args = parser.parse_args(argv)

    from ..config import get_settings
    from ..db import create_all, init_engine, new_session
    from ..models import Tenant
    from ..rag.retrieval import cosine_gate, hybrid_search
    from ..seed import seed_demo
    from ..services import Services

    settings = get_settings()
    settings.database_url = f"sqlite:///{Path(tempfile.mkdtemp()) / 'gate.db'}"
    init_engine(settings.database_url)
    create_all()
    services = Services(settings)
    probe = json.loads(PROBE_FILE.read_text(encoding="utf-8"))
    report: dict = {"min_relevance": settings.min_relevance}
    with new_session() as db:
        seed_demo(db, services.embedder)
        tenant = db.execute(select(Tenant).where(Tenant.slug == args.tenant)).scalar_one()

        def passes(question: str) -> tuple[bool, float, str]:
            hits, _ = hybrid_search(db, tenant.id, question, services.embedder, {"public"},
                                    min_relevance=settings.min_relevance, min_cosine=cosine_gate(settings))
            best = max(hits, key=lambda h: h.relevance, default=None)
            return bool(hits), round(best.relevance, 3) if best else 0.0, best.heading if best else ""

        for name, want in (("in_scope", True), ("out_of_scope", False)):
            misses = []
            for question in probe[name]:
                ok, relevance, heading = passes(question)
                if ok != want:
                    misses.append({"question": question, "relevance": relevance, "matched": heading})
            total = len(probe[name])
            report[name] = {"correct": total - len(misses), "total": total, "misses": misses}

        if args.calibrate:  # N-030: calibrate the vector gate for the embedder in use
            def best_cosine(question: str) -> float:
                hits, _ = hybrid_search(db, tenant.id, question, services.embedder, {"public"}, top_k=8,
                                        min_relevance=0.0, min_cosine=0.0)
                return round(max((h.cosine for h in hits), default=0.0), 4)

            inside = sorted(best_cosine(q) for q in probe["in_scope"])
            outside = sorted(best_cosine(q) for q in probe["out_of_scope"])
            # Just above the most similar out-of-scope question: none of them passes on cosine alone.
            suggested = round(min(1.0, outside[-1] + 0.02), 3)
            report["calibration"] = {
                "embedder": settings.embedding_provider + (f":{settings.embedding_model}" if settings.embedding_model else ""),
                "in_scope_best_cosine": {"min": inside[0], "median": inside[len(inside) // 2]},
                "out_of_scope_best_cosine": {"median": outside[len(outside) // 2], "max": outside[-1]},
                "suggested_min_cosine": suggested,
                "in_scope_passing_on_cosine_alone": f"{sum(c >= suggested for c in inside)}/{len(inside)}",
            }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
