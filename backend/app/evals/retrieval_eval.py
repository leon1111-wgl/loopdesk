"""Compare retrieval configurations on every measurement LoopDesk has (W5).

    python -m app.evals.retrieval_eval --report out/retrieval-eval.json --summary out/summary.md
    python -m app.evals.retrieval_eval --configs lexical,rerank+floor --tenants leon

Configurations, each on a fresh scratch database seeded from scratch:

- ``lexical``: BM25 + hashed vectors + the coverage gate (the offline default);
- ``lexical+questions``: + the published colloquial index questions (``--questions``);
- ``embed``: the semantic embedder (LOOPDESK_EMBEDDING_*, e.g. bge-m3), gate = coverage OR cosine;
- ``rerank``: + the reranker (LOOPDESK_RERANKER_*), gate = coverage OR rerank score;
- ``rerank+floor``: + the reranker's veto of a coverage pass;
- ``full``: + the index questions.

Semantic thresholds are calibrated per configuration on the probe's dev split only
(app.evals.gate_probe.calibrate); the held-out half, the blind probe set (evals/gate_probe_blind.json),
the paraphrase set, the hard suite and every tenant's eval gate are measured with them. Every
proposal in the questions file is published unedited (a reviewer who accepts everything), so the
number is not shaped by someone who has seen the eval sets. Latency is wall-clock on the machine it
runs on: the workflow (.github/workflows/retrieval-eval.yml) runs it on a GitHub-hosted CPU runner.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import statistics
import time
from pathlib import Path

from sqlalchemy import select

from .scratch import scratch_database_url

EVALS = Path(__file__).resolve().parents[2] / "evals"
BLIND_FILE = EVALS / "gate_probe_blind.json"
QUESTIONS_FILE = EVALS / "index_questions.json"

CONFIGS = {
    "lexical": {"semantic": False, "rerank": False, "floor": False, "questions": False},
    "lexical+questions": {"semantic": False, "rerank": False, "floor": False, "questions": True},
    "embed": {"semantic": True, "rerank": False, "floor": False, "questions": False},
    "rerank": {"semantic": True, "rerank": True, "floor": False, "questions": False},
    "rerank+floor": {"semantic": True, "rerank": True, "floor": True, "questions": False},
    "full": {"semantic": True, "rerank": True, "floor": True, "questions": True},
}


class Timed:
    """Wall-clock per model call, kept by kind, and a memo of query vectors and rerank scores: the
    measurements ask the same question several times (gated and ungated, calibration and probe), and
    a deterministic model gives the same answer, so only the first call runs the model. Latency is
    measured apart (latency_bench) with the memo off."""

    def __init__(self, inner, log: dict[str, list[float]]):
        self.inner = inner
        self.log = log
        self.dim = getattr(inner, "dim", None)
        self.name = getattr(inner, "name", type(inner).__name__)
        self.memo = True
        self._queries: dict[str, list[float]] = {}
        self._scores: dict[tuple[str, str], float] = {}

    def _timed(self, kind, call):
        started = time.perf_counter()
        try:
            return call()
        finally:
            self.log.setdefault(kind, []).append(time.perf_counter() - started)

    def embed(self, texts):
        return self._timed("embed", lambda: self.inner.embed(texts))

    def embed_query(self, text):
        if self.memo and text in self._queries:
            return self._queries[text]
        vector = self._timed("embed_query", lambda: self.inner.embed_query(text))
        self._queries[text] = vector
        return vector

    def score(self, query, passages):
        if self.memo and all((query, p) in self._scores for p in passages):
            return [self._scores[(query, p)] for p in passages]
        scores = self._timed("rerank", lambda: self.inner.score(query, passages))
        self.log.setdefault("rerank_passages", []).append(len(passages))
        self._scores.update(((query, p), v) for p, v in zip(passages, scores))
        return scores


def latency_bench(db, questions: list[tuple[int, str]], embedder: Timed, reranker: Timed | None, gate: dict,
                  log: dict[str, list[float]]) -> dict:
    """One full search per question, memo off, one at a time: what a customer waits for on this CPU."""
    from ..rag.retrieval import hybrid_search

    for kind in ("embed_query", "rerank", "rerank_passages"):
        log.pop(kind, None)
    embedder.memo = False
    if reranker is not None:
        reranker.memo = False
    searches = []
    try:
        for tenant_id, question in questions:
            started = time.perf_counter()
            hybrid_search(db, tenant_id, question, embedder, {"public"}, reranker=reranker, **gate)
            searches.append(time.perf_counter() - started)
    finally:
        embedder.memo = True
        if reranker is not None:
            reranker.memo = True
    passages = log.get("rerank_passages", [])
    return {"questions": len(questions), "search": _stats(searches), "embed_query": _stats(log.get("embed_query", [])),
            "rerank": _stats(log.get("rerank", [])),
            "rerank_passages_mean": round(statistics.fmean(passages), 1) if passages else None}


def rerank_scaling(reranker: Timed, pairs: list[tuple[str, list[str]]], sizes=(5, 10, 20)) -> dict:
    """Cross-encoder time by the number of candidates it reads (the cost knob rerank_candidates)."""
    out = {}
    reranker.memo = False
    try:
        for n in sizes:
            times = []
            for query, passages in pairs:
                if len(passages) < n:
                    continue
                started = time.perf_counter()
                reranker.inner.score(query, passages[:n])
                times.append(time.perf_counter() - started)
            out[str(n)] = _stats(times)
    finally:
        reranker.memo = True
    return out


def _stats(seconds: list[float]) -> dict:
    if not seconds:
        return {"n": 0}
    ms = sorted(s * 1000 for s in seconds)
    return {"n": len(ms), "p50": round(statistics.median(ms), 1), "p95": round(ms[min(len(ms) - 1, int(len(ms) * 0.95))], 1),
            "mean": round(statistics.fmean(ms), 1), "max": round(ms[-1], 1)}


def seeded_chunk_texts(slug: str) -> dict[str, str]:
    """heading -> chunk text of a tenant's seeded documents (one chunk per heading in the demo data)."""
    from ..rag.chunking import chunk_markdown
    from ..seed import SEED_DIR, TENANTS

    spec = next(t for t in TENANTS if t["slug"] == slug)
    return {d.heading: d.text for filename, _, _ in spec["documents"]
            for d in chunk_markdown((SEED_DIR / slug / filename).read_text(encoding="utf-8"))}


def _section_match(heading: str, item: dict) -> bool:
    parts = heading.split(" > ")[1:]
    return any(s in parts for s in (item["section"], *item.get("also", [])))


def _scaling_pairs(db, tenant_id: int, probe: dict | None, embedder, settings, limit: int = 8) -> list:
    """(question, the 20 fused candidates' passages) for a few blind in-scope questions."""
    from ..rag.retrieval import hybrid_search

    pairs = []
    for item in (probe or {}).get("in_scope", [])[:limit]:
        hits, _ = hybrid_search(db, tenant_id, item["q"], embedder, {"public"}, top_k=20, min_relevance=0.0,
                                min_cosine=0.0, index_questions=settings.index_questions)
        pairs.append((item["q"], [f"{h.heading}\n{h.text}" for h in hits]))
    return pairs


def _blind(db, tenant_id: int, probe: dict, embedder, reranker, gate: dict) -> dict:
    from .gate_probe import probe as run_probe
    from .paraphrase import measure_on

    inside = measure_on(db, tenant_id, embedder, reranker, gate, probe["in_scope"], _section_match)
    outside = run_probe(db, tenant_id, {"in_scope": [], "out_of_scope": probe["out_of_scope"]}, embedder, reranker, gate)
    ambiguous = run_probe(db, tenant_id, {"in_scope": [], "out_of_scope": probe.get("ambiguous", [])},
                          embedder, reranker, gate)
    return {"in_scope": {k: inside[k] for k in ("total", "answered", "refused", "wrong", "recall_at_4")},
            "out_of_scope_refused": f"{outside['out_of_scope']['correct']}/{outside['out_of_scope']['total']}",
            "ambiguous_refused": f"{ambiguous['out_of_scope']['correct']}/{ambiguous['out_of_scope']['total']}",
            "misses": {"in_scope": inside["misses"], "out_of_scope": outside["out_of_scope"]["misses"]}}


def _publish_all(db, tenants, data: dict | None) -> dict[str, int]:
    """Import the questions file as proposals and publish every one of them unedited."""
    from ..flywheel.service import apply_patch
    from ..models import IndexQuestion, User
    from ..rag.questions import import_questions
    from ..services import get_services

    counts: dict[str, int] = {}
    by_slug = {t.get("tenant"): t for t in (data or {}).get("tenants", [])}
    for tenant in tenants:
        reviewer = db.execute(select(User).where(User.tenant_id == tenant.id, User.role == "admin")).scalars().first()
        for patch in import_questions(db, tenant, by_slug.get(tenant.slug) or {}):
            apply_patch(db, patch, reviewer, get_services().embedder)
        counts[tenant.slug] = len(db.execute(select(IndexQuestion.id).where(IndexQuestion.tenant_id == tenant.id)).all())
    return counts


def run_config(name: str, tenants: list[str] | None = None, suites: bool = True, probes: bool = True,
               questions_file: Path | None = None, embedder_factory=None, reranker_factory=None,
               latency_questions: int = 30) -> dict:
    from ..config import Settings
    from ..db import create_all, init_engine, new_session
    from ..models import Chunk, EvalCase, Tenant
    from ..rag.embeddings import build_embedder
    from ..rag.rerank import build_reranker
    from ..rag.retrieval import gate_options, index_manager
    from ..seed import seed_demo
    from ..services import Services, set_services
    from .gate_probe import calibrate, candidates, load_probe, probe
    from .paraphrase import measure_on
    from .runner import run_suite

    spec = CONFIGS[name]
    settings = Settings.from_env()
    if not spec["semantic"]:
        settings.embedding_provider, settings.embedding_model, settings.embedding_dim = "hashing", None, 512
    settings.reranker_provider = settings.reranker_provider if spec["rerank"] else "none"
    settings.min_cosine = settings.min_rerank = settings.rerank_floor = None
    settings.index_questions = spec["questions"]
    settings.database_url = scratch_database_url(f"retrieval-{name.replace('+', '-')}")
    init_engine(settings.database_url)
    create_all()
    index_manager.clear()  # tenant ids repeat in every scratch database

    log: dict[str, list[float]] = {}
    services = Services(settings)
    raw_embedder = (embedder_factory or build_embedder)(settings) if spec["semantic"] else services.embedder
    services.embedder = Timed(raw_embedder, log)
    reranker = None
    if spec["rerank"]:
        built = (reranker_factory or build_reranker)(settings)
        if built is None:
            raise SystemExit(f"config {name} needs a reranker: set LOOPDESK_RERANKER_PROVIDER and _MODEL")
        reranker = Timed(built, log)
    services.reranker = reranker
    set_services(services)

    result: dict = {"embedder": settings.embedding_provider + (f":{settings.embedding_model}" if spec["semantic"] and
                                                                  settings.embedding_model else ""),
                    "reranker": reranker.name if reranker else None}
    warm = time.perf_counter()  # loading a local model is a one-off cost, kept apart from the per-call numbers
    services.embedder.inner.embed(["预热"])
    if reranker is not None:
        reranker.inner.score("预热", ["预热"])
    result["model_load_seconds"] = round(time.perf_counter() - warm, 2)

    with new_session() as db:
        started = time.perf_counter()
        seed_demo(db, services.embedder)
        seed_seconds = time.perf_counter() - started
        all_tenants = db.execute(select(Tenant).order_by(Tenant.id)).scalars().all()
        chosen = [t for t in all_tenants if not tenants or t.slug in tenants]
        published = {}
        if spec["questions"]:
            path = questions_file or QUESTIONS_FILE
            data = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else None
            published = _publish_all(db, all_tenants, data)
        index_manager.clear()
        started = time.perf_counter()
        for tenant in all_tenants:
            index_manager.get(db, tenant.id)
        result["index"] = {
            "chunks": len(db.execute(select(Chunk.id)).all()),
            "seed_seconds": round(seed_seconds, 2),
            "embed_seconds": round(sum(log.get("embed", [])), 2),
            "build_seconds": round(time.perf_counter() - started, 3),
            "questions": {k: v for k, v in published.items() if not tenants or k in tenants},
        }
        log.pop("embed_query", None)

        gate = gate_options(settings)
        result["calibration"] = None
        leon = next((t for t in all_tenants if t.slug == "leon"), None)
        if spec["semantic"] and leon is not None:
            dev = load_probe("leon", "dev")
            before = len(log.get("rerank", []))
            scores = {k: [candidates(db, leon.id, q, services.embedder, reranker, settings.rerank_candidates,
                                     settings.index_questions) for q in dev[k]] for k in ("in_scope", "out_of_scope")}
            calibration = calibrate(scores["in_scope"], scores["out_of_scope"], settings.min_relevance)
            calibration["queries"] = len(log.get("rerank", [])) - before
            result["calibration"] = calibration
            if spec["rerank"]:  # two signals: coverage OR the cross-encoder; the cosine only recalls
                gate.update(min_cosine=None, min_rerank=calibration["min_rerank"],
                            rerank_floor=calibration["rerank_floor"] if spec["floor"] else None)
            else:
                gate.update(min_cosine=calibration["min_cosine"])
        result["settings"] = gate
        settings.min_cosine, settings.min_rerank, settings.rerank_floor = (gate["min_cosine"], gate["min_rerank"],
                                                                           gate["rerank_floor"])
        log.pop("embed_query", None)
        log.pop("rerank", None)

        blind = json.loads(BLIND_FILE.read_text(encoding="utf-8"))["tenants"] if BLIND_FILE.exists() else {}
        if probes:
            if leon is not None and (not tenants or "leon" in tenants):
                result["probe"] = {}
                for split in ("dev", "heldout"):
                    found = probe(db, leon.id, load_probe("leon", split), services.embedder, reranker, gate)
                    result["probe"][split] = {
                        "in_scope": f"{found['in_scope']['correct']}/{found['in_scope']['total']}",
                        "out_of_scope": f"{found['out_of_scope']['correct']}/{found['out_of_scope']['total']}",
                        "misses": found["in_scope"]["misses"] + found["out_of_scope"]["misses"]}
                result["probe"]["all"] = {k: _sum([result["probe"][s][k] for s in ("dev", "heldout")])
                                          for k in ("in_scope", "out_of_scope")}
                result["paraphrase"] = measure_on(db, leon.id, services.embedder, reranker, gate)
            result["blind"] = {t.slug: _blind(db, t.id, blind[t.slug], services.embedder, reranker, gate)
                               for t in chosen if t.slug in blind}
            # A fixed sample of the blind set's questions, every tenant in turn.
            per_tenant = [[(t.id, q["q"]) for q in blind[t.slug]["in_scope"]] for t in chosen if t.slug in blind]
            sample = [pair for group in zip(*per_tenant) for pair in group] if per_tenant else []
            if sample:
                result["latency_ms"] = latency_bench(db, sample[:latency_questions], services.embedder, reranker,
                                                     gate, log)
            if reranker is not None and leon is not None:
                result["rerank_scaling_ms"] = rerank_scaling(reranker, _scaling_pairs(db, leon.id, blind.get("leon"),
                                                                                       services.embedder, settings))

        if suites:
            result["gates"], result["hard"] = {}, {}
            for tenant in chosen:
                for suite, into in (("default", result["gates"]), ("hard", result["hard"])):
                    has_cases = db.execute(select(EvalCase.id).where(EvalCase.tenant_id == tenant.id,
                                                                     EvalCase.suite == suite)).first()
                    if not has_cases:
                        continue
                    run = run_suite(db, services, tenant, suite, triggered_by=f"retrieval:{name}")
                    m = run.metrics
                    into[tenant.slug] = {"passed": run.passed, "total": run.total, "pass_rate": m["pass_rate"],
                                         "hit_at_1": m.get("retrieval_hit_at_1"), "mrr": m.get("retrieval_mrr"),
                                         "refusal": m["checks"].get("refusal"), "latency_p50_ms": m["latency_p50_ms"],
                                         "latency_p95_ms": m["latency_p95_ms"]}
    index_manager.clear()
    return result


def run(configs: list[str], tenants: list[str] | None = None, suites: bool = True, probes: bool = True,
        questions_file: Path | None = None, embedder_factory=None, reranker_factory=None) -> dict:
    unknown = [c for c in configs if c not in CONFIGS]
    if unknown:
        raise ValueError(f"unknown configuration(s) {unknown}; known: {list(CONFIGS)}")
    report = {"environment": _environment(), "configs": {}}
    for name in configs:
        report["configs"][name] = run_config(name, tenants, suites, probes, questions_file, embedder_factory,
                                             reranker_factory)
    return report


def _environment() -> dict:
    env = {"python": platform.python_version(), "machine": platform.machine(), "cpus": os.cpu_count(),
           "processor": platform.processor() or platform.machine()}
    with contextlib.suppress(Exception):
        import torch  # only where requirements-semantic.txt is installed

        env["torch"] = torch.__version__
        env["torch_threads"] = torch.get_num_threads()
    return env


def _frac(value: str | None) -> tuple[int, int]:
    if not value:
        return 0, 0
    a, b = value.split("/")
    return int(a), int(b)


def _sum(values: list[str]) -> str:
    pairs = [_frac(v) for v in values if v]
    return f"{sum(a for a, _ in pairs)}/{sum(b for _, b in pairs)}" if pairs else "—"


def summary(report: dict) -> str:
    """Markdown tables for the job summary and docs/EVALUATION.md."""
    rows = ["| config | probe dev in / out | probe held-out in / out | probe all in / out | blind in answered / out refused | "
            "paraphrase answered (recall@4) | hard | gates | search p50 / p95 ms | rerank p50 ms | thresholds |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, c in report["configs"].items():
        probe = c.get("probe") or {}
        dev, held, every = probe.get("dev") or {}, probe.get("heldout") or {}, probe.get("all") or {}
        blind = c.get("blind") or {}
        para = c.get("paraphrase") or {}
        gates = c.get("gates") or {}
        hard = c.get("hard") or {}
        lat = c.get("latency_ms") or {}
        s = c.get("settings") or {}
        thresholds = ", ".join(f"{k}={s[k]}" for k in ("min_cosine", "min_rerank", "rerank_floor") if s.get(k) is not None)
        rows.append("| " + " | ".join([
            name,
            f"{dev.get('in_scope', '—')} / {dev.get('out_of_scope', '—')}",
            f"{held.get('in_scope', '—')} / {held.get('out_of_scope', '—')}",
            f"{every.get('in_scope', '—')} / {every.get('out_of_scope', '—')}",
            f"{_sum([b['in_scope']['answered'] for b in blind.values()])} / {_sum([b['out_of_scope_refused'] for b in blind.values()])}",
            f"{para.get('answered', '—')} ({para.get('recall_at_4', '—')})",
            ", ".join(f"{k} {v['passed']}/{v['total']}" for k, v in hard.items()) or "—",
            ", ".join(f"{k} {v['passed']}/{v['total']}" for k, v in gates.items()) or "—",
            f"{lat.get('search', {}).get('p50', '—')} / {lat.get('search', {}).get('p95', '—')}",
            str(lat.get("rerank", {}).get("p50", "—")),
            thresholds or "—",
        ]) + " |")
    lines = ["## Retrieval configurations", "", *rows, "", "## Blind probe set per tenant", "",
             "| config | tenant | in-scope answered | refused | wrong section | out-of-scope refused | ambiguous refused |",
             "|---|---|---|---|---|---|---|"]
    for name, c in report["configs"].items():
        for slug, b in (c.get("blind") or {}).items():
            lines.append(f"| {name} | {slug} | {b['in_scope']['answered']} | {b['in_scope']['refused']} | "
                         f"{b['in_scope']['wrong']} | {b['out_of_scope_refused']} | {b['ambiguous_refused']} |")
    lines += ["", "## Index and models", "", "| config | embedder | reranker | chunks | questions | model load s | "
              "seed s (embed s) | index build s | embed query p50 ms |", "|---|---|---|---|---|---|---|---|---|"]
    for name, c in report["configs"].items():
        i = c.get("index") or {}
        lines.append(f"| {name} | {c.get('embedder')} | {c.get('reranker') or '—'} | {i.get('chunks')} | "
                     f"{sum((i.get('questions') or {}).values())} | {c.get('model_load_seconds')} | "
                     f"{i.get('seed_seconds')} ({i.get('embed_seconds')}) | {i.get('build_seconds')} | "
                     f"{(c.get('latency_ms') or {}).get('embed_query', {}).get('p50', '—')} |")
    env = report.get("environment", {})
    lines += ["", f"Environment: {json.dumps(env, ensure_ascii=False)}", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoopDesk retrieval configurations")
    parser.add_argument("--configs", default=",".join(CONFIGS))
    parser.add_argument("--tenants", help="comma-separated slugs (default: every seeded tenant)")
    parser.add_argument("--questions", help=f"index questions file (default: {QUESTIONS_FILE.name})")
    parser.add_argument("--no-suites", action="store_true", help="skip the eval gates and the hard suite")
    parser.add_argument("--merge", nargs="+", help="combine reports of separate runs (one per configuration)")
    parser.add_argument("--report")
    parser.add_argument("--summary")
    args = parser.parse_args(argv)
    if args.merge:
        report = {"environment": {}, "configs": {}}
        for path in args.merge:
            part = json.loads(Path(path).read_text(encoding="utf-8"))
            report["environment"] = report["environment"] or part.get("environment", {})
            report["configs"].update(part["configs"])
        order = list(CONFIGS)
        report["configs"] = dict(sorted(report["configs"].items(), key=lambda kv: order.index(kv[0])))
    else:
        report = run([c.strip() for c in args.configs.split(",") if c.strip()],
                     tenants=[t.strip() for t in args.tenants.split(",")] if args.tenants else None,
                     suites=not args.no_suites, questions_file=Path(args.questions) if args.questions else None)
    text = summary(report)
    print(text)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.summary:
        Path(args.summary).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
