"""The retrieval evaluation that compares configurations (W5): lexical baseline, + embeddings,
+ rerank, + the reranker's veto floor, + index questions. The real models run on GitHub Actions
(.github/workflows/retrieval-eval.yml); here the semantic parts are fakes, and the lexical baseline
must reproduce the numbers docs/EVALUATION.md reports for it."""

import json

from sqlalchemy import select

from app.evals import retrieval_eval
from app.evals.gate_probe import load_probe
from app.rag.rerank import Reranker


class Recording(Reranker):
    name = "recording"

    def __init__(self):
        self.queries: list[str] = []

    def score(self, query, passages):
        self.queries.append(query)
        return [0.9 if "退" in query and "退" in p else 0.05 for p in passages]


def test_the_lexical_baseline_reproduces_the_documented_numbers():
    report = retrieval_eval.run(["lexical"], tenants=["leon"], suites=False)
    lexical = report["configs"]["lexical"]
    assert lexical["probe"]["all"]["in_scope"] == "33/33"
    assert lexical["probe"]["all"]["out_of_scope"] == "16/20"
    assert lexical["paraphrase"]["answered"] == "27/36" and lexical["paraphrase"]["recall_at_4"] == "33/36"
    assert lexical["calibration"] is None, "nothing semantic to calibrate"
    assert lexical["index"]["chunks"] > 0 and lexical["index"]["build_seconds"] >= 0
    assert lexical["latency_ms"]["search"]["n"] == 20, "leon's blind questions (30 at most, tenants in turn)"


def test_semantic_thresholds_are_calibrated_on_dev_questions_only():
    reranker = Recording()
    report = retrieval_eval.run(["rerank+floor"], tenants=["leon"], suites=False,
                                reranker_factory=lambda settings: reranker)
    config = report["configs"]["rerank+floor"]
    dev, heldout = load_probe("leon", "dev"), load_probe("leon", "heldout")
    assert reranker.queries[0] == "预热", "a warm-up call loads the model before anything is timed"
    calibration_queries = reranker.queries[1:1 + config["calibration"]["queries"]]
    assert len(calibration_queries) >= 20
    assert set(calibration_queries) <= set(dev["in_scope"] + dev["out_of_scope"])
    assert not set(calibration_queries) & set(heldout["in_scope"] + heldout["out_of_scope"])
    gate = config["settings"]
    assert gate["min_rerank"] == config["calibration"]["min_rerank"]
    assert gate["rerank_floor"] == config["calibration"]["rerank_floor"]
    assert set(config["probe"]) >= {"dev", "heldout", "all"}
    assert config["latency_ms"]["rerank"]["n"] > 0


def test_a_config_with_questions_publishes_the_proposals_from_the_file(tmp_path):
    from app.db import new_session
    from app.models import Chunk, Tenant
    from app.rag.questions import text_sha

    # The file the offline generator wrote; bound to the seeded text of one section.
    seeded = retrieval_eval.seeded_chunk_texts("leon")
    heading = "配送与物流说明 > 合作快递"
    data = {"tenant": "leon", "documents": [{"title": "配送与物流说明", "items": [
        {"heading": heading, "text_sha": text_sha(seeded[heading]), "questions": ["包裹到哪儿了在哪查"]}]}]}
    path = tmp_path / "questions.json"
    path.write_text(json.dumps({"tenants": [data]}, ensure_ascii=False))
    report = retrieval_eval.run(["lexical+questions"], tenants=["leon"], suites=False, questions_file=path)
    assert report["configs"]["lexical+questions"]["index"]["questions"] == {"leon": 1}


def test_every_tenant_gate_runs_under_each_config():
    report = retrieval_eval.run(["lexical"], tenants=["iris"], probes=False)
    gate = report["configs"]["lexical"]["gates"]["iris"]
    assert gate["total"] >= 10 and gate["passed"] == gate["total"]


def test_the_summary_is_a_table_per_metric():
    report = retrieval_eval.run(["lexical"], tenants=["leon"], suites=False)
    summary = retrieval_eval.summary(report)
    assert "| lexical |" in summary and "33/33" in summary and "27/36" in summary


def test_round_two_calibrates_on_everything_seen_and_measures_a_fresh_blind_set(tmp_path):
    import pytest

    fresh = {"tenants": {"leon": {"in_scope": [{"q": "退款啥时候到", "section": "退款到账时间"}],
                                  "out_of_scope": ["你们周末发货吗"]}}}
    path = tmp_path / "blind_v2.json"
    path.write_text(json.dumps(fresh, ensure_ascii=False))
    reranker = Recording()
    report = retrieval_eval.run(["rerank+floor"], tenants=["leon"], suites=False, calibrate_on="pool",
                                blind_file=path, reranker_factory=lambda settings: reranker)
    config = report["configs"]["rerank+floor"]
    assert config["calibration"]["rule"] == "constrained" and config["calibrate_on"] == "pool"
    seen = set(reranker.queries[1:1 + config["calibration"]["queries"]])
    assert "退款啥时候到" not in seen and "你们周末发货吗" not in seen, "the fresh set stays out of the pool"
    assert set(load_probe("leon", "heldout")["in_scope"]) <= seen, "round 2 pools the whole probe set"
    assert config["blind"]["leon"]["in_scope"]["total"] == 1 and config["blind_file"] == "blind_v2.json"
    with pytest.raises(SystemExit):
        retrieval_eval.run(["rerank+floor"], tenants=["leon"], suites=False, calibrate_on="pool",
                           reranker_factory=lambda settings: Recording())
