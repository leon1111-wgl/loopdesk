"""Probe the retrieval relevance gate: does it let in-scope questions through and keep
out-of-scope ones out?

    python -m app.evals.gate_probe --report gate.json

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
    args = parser.parse_args(argv)

    from ..config import get_settings
    from ..db import create_all, init_engine, new_session
    from ..models import Tenant
    from ..rag.retrieval import hybrid_search
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
                                    min_relevance=settings.min_relevance)
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

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
