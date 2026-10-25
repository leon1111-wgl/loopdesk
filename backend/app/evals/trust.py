"""Replay recorded real-model answers through the answer check and score it against blind labels.

    python -m app.evals.trust                         # tiered check, rules, no judge: every split
    python -m app.evals.trust --mode lexical          # the previous check, for the "before" numbers
    python -m app.evals.trust --judge                 # tiered, with the configured model judging disagreements
    python -m app.evals.trust --classifier model      # the fine-tuned claim classifier (LOOPDESK_CLAIM_MODEL_DIR)
    python -m app.evals.trust --report trust.json

Data (backend/evals/trust/, synthetic tenants):
- recorded.jsonl: 937 answers DeepSeek V4.1 Flash wrote on 2026-10-20 (Leon, Iris and the hard suite, prompt
  versions 1.00-1.02, 3 repeats), each with the evidence the agent had: what the customer saw, replayed here
  without calling the model again;
- labels.jsonl: per sentence, claim or not and supported or not, labelled blind by another model (Fable) that
  saw the question and the evidence but not the verifier or its verdicts;
- planted.jsonl: one wrong sentence planted in a correct answer, written blind by another model in nine
  categories (number, order id, status, negation, party, condition, invented, promise, time unit).
The split is by case: held-out cases never tuned a rule, a threshold or the claim model.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "evals" / "trust"


def load(name: str) -> list[dict]:
    path = DATA / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0,
            "tp": tp, "fp": fp, "fn": fn}


class OracleJudge:
    """An upper bound, not a result: a judge that answers every disputed claim the way the blind labels do."""

    name = "oracle"

    def __init__(self, labels: dict):
        self.labels, self.group, self.wrong = labels, "", ""

    def chat(self, messages, tools, model, options=None):
        import re

        from ..llm.base import ChatResult, Usage
        listed = messages[-1].content.split("句子（按编号）：")[1]
        texts = re.findall(r"^\d+\. (.*)$", listed, re.M)
        def verdict(text: str) -> bool:
            if self.wrong:  # a planted answer: only the planted sentence is wrong
                return not (text in self.wrong or self.wrong in text)
            label = self.labels.get((self.group, text)) or {}
            return label.get("supported") is not False  # a non-claim cannot be wrong
        verdicts = [verdict(t) for t in texts]
        return ChatResult(json.dumps({"supported": verdicts}), [], Usage(), model)


def run(mode: str, splits: list[str], judge=None, judge_model: str = "", classifier: str = "rules",
        model_dir: str | None = None, workers: int = 1) -> dict:
    from ..agent.status_templates import case_state, compose
    from ..agent.tools import Evidence
    from ..agent.verifier import plan_repair, verify_answer
    from ..config import Settings

    settings = Settings()
    settings.verifier = mode
    settings.claim_classifier, settings.claim_model_dir = classifier, model_dir
    labels = {(l["group"], l["text"]): l for l in load("labels.jsonl")}
    if judge == "oracle":
        judge = OracleJudge(labels)
    groups = load("recorded.jsonl")
    planted = load("planted.jsonl") if (DATA / "planted.jsonl").exists() else []
    contexts = {p["ctx"]: p for p in load("planted_contexts.jsonl")} if planted else {}

    report: dict = {"mode": mode, "classifier": classifier, "judge": getattr(judge, "name", None), "splits": {}}
    for split in splits:
        claim = Counter()
        support = Counter()
        answers = Counter()
        judge_calls = judge_sentences = disputed_answers = disputed_claims = claims_checked = 0
        latency: list[float] = []
        unmatched = 0
        judge_ms: list[int] = []

        def check(group: dict, record: dict):
            evidence = [Evidence(**e) for e in group["evidence"]]
            started = time.perf_counter()
            text, templates = record["answer"], []
            if mode == "tiered":  # as the runtime does: status sentences from the tools first (eval runs are drills)
                composed = compose(text, case_state(evidence, group["question"]), dry_run=True)
                text, templates = composed.text, composed.templates
            if isinstance(judge, OracleJudge):
                judge.group = group["group"]
            v = verify_answer(text, evidence, group["question"], settings, judge, judge_model, trusted=templates)
            return text, v, (time.perf_counter() - started) * 1000

        todo = [(g, r) for g in groups if split == "all" or g["split"] == split for r in g["answers"]]
        if workers > 1 and not isinstance(judge, OracleJudge):  # a model judge: calls in parallel
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(workers) as pool:
                done = list(pool.map(lambda gr: check(*gr), todo))
        else:
            done = [check(g, r) for g, r in todo]
        for (group, record), (text, v, ms) in zip(todo, done):
            if True:
                latency.append(ms)
                if v.judge_calls:
                    judge_ms.append(v.judge_ms)
                judge_calls += v.judge_calls
                judge_sentences += v.judged
                disputed_answers += bool(v.disputed)
                disputed_claims += v.disputed
                claims_checked += sum(1 for c in v.sentences if c.factual and c.source != "template")
                gold_bad = False
                for check in v.sentences:
                    label = labels.get((group["group"], check.text))
                    if label is None:
                        unmatched += 1
                        continue
                    claim[("tp" if check.factual else "fn") if label["claim"] else ("fp" if check.factual else "tn")] += 1
                    if not label["claim"]:
                        continue
                    failed = check.factual and not check.supported
                    if label["supported"]:
                        support["supported"] += 1
                        support["false_fail"] += failed
                    else:
                        gold_bad = True
                        support["unsupported"] += 1
                        support["caught"] += failed
                plan = plan_repair(v, text)
                key = "bad" if gold_bad else "ok"
                answers[f"{key}_total"] += 1
                answers[f"{key}_{plan.action}"] += 1
                answers[f"{key}_verdict_{v.verdict}"] += 1
                if gold_bad and plan.action in ("pass",):
                    answers["bad_shown"] += 1
                if gold_bad and plan.action == "fix":
                    # caught when every wrong sentence is among the dropped ones
                    wrong = {c.text for c in v.sentences if (lb := labels.get((group["group"], c.text)))
                             and lb["claim"] and not lb["supported"]}
                    answers["bad_shown" if wrong - set(plan.dropped) else "bad_fixed_clean"] += 1
        latency.sort()
        report["splits"][split] = {
            "claim": _prf(claim["tp"], claim["fp"], claim["fn"]),
            "support": {**support, "false_fail_rate": round(support["false_fail"] / max(1, support["supported"]), 4),
                        "miss": support["unsupported"] - support["caught"]},
            "answers": dict(answers),
            "judge_calls": judge_calls, "judge_sentences": judge_sentences,
            "disputed_answers": disputed_answers, "disputed_claims": disputed_claims, "claims_checked": claims_checked,
            "judge_ms_p50": sorted(judge_ms)[len(judge_ms) // 2] if judge_ms else None,
            "judge_ms_p95": sorted(judge_ms)[int(len(judge_ms) * 0.95)] if judge_ms else None,
            "judge_failed": sum(1 for _, v, _ in done if v.judge_calls and v.judge_failed),
            "answers_checked": len(latency),
            "check_ms_p50": round(latency[len(latency) // 2], 2) if latency else None,
            "check_ms_p95": round(latency[int(len(latency) * 0.95)], 2) if latency else None,
            "unmatched_sentences": unmatched,
        }
    if planted:
        report["planted"] = planted_report(planted, contexts, settings, judge, judge_model, splits)
        if isinstance(judge, OracleJudge):
            judge.wrong = ""
    return report


def planted_report(planted: list[dict], contexts: dict, settings, judge, judge_model: str, splits: list[str]) -> dict:
    """A planted sentence is caught when the check fails it: it is dropped, re-asked or handed over, never shown."""
    from ..agent.tools import Evidence
    from ..agent.verifier import plan_repair, verify_answer
    from ..textutil import strip_citations

    by_cat: dict[str, Counter] = defaultdict(Counter)
    missed = []
    for p in planted:
        ctx = contexts[p["ctx"]]
        if "all" not in splits and ctx["split"] not in splits:
            continue
        evidence = [Evidence(**e) for e in ctx["evidence"]]
        wrong = strip_citations(p["wrong_sentence"])[0].strip()
        if getattr(judge, "name", "") == "oracle":
            judge.wrong = wrong
        v = verify_answer(p["answer"], evidence, ctx["question"], settings, judge, judge_model)
        original = strip_citations(p.get("original_sentence") or "")[0]
        # the checked pieces of the planted sentence that differ from the sentence it replaced
        hit = [c for c in v.sentences if c.text and (c.text in wrong or wrong in c.text)
               and not (original and c.text in original)]
        caught = bool(hit) and all(c.factual and not c.supported for c in hit)
        plan = plan_repair(v, p["answer"])
        by_cat[p["category"]]["total"] += 1
        by_cat[p["category"]]["caught"] += caught
        by_cat[p["category"]][plan.action] += 1
        if not caught:
            missed.append({"id": p["id"], "category": p["category"], "sentence": p["wrong_sentence"],
                           "checks": [(c.text, c.factual, c.supported, c.score, c.reason) for c in hit]})
    total = sum(c["total"] for c in by_cat.values())
    caught = sum(c["caught"] for c in by_cat.values())
    return {"total": total, "caught": caught, "by_category": {k: dict(v) for k, v in sorted(by_cat.items())},
            "missed": missed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay recorded answers through the answer check")
    parser.add_argument("--mode", default="tiered", choices=["lexical", "tiered"])
    parser.add_argument("--split", default="dev,heldout,all")
    parser.add_argument("--judge", nargs="?", const="model", choices=["model", "oracle"],
                        help="tiered: the configured model judges disagreements (oracle: the labels do; an upper bound)")
    parser.add_argument("--classifier", default="rules", choices=["rules", "model"])
    parser.add_argument("--model-dir", help="the claim model (LOOPDESK_CLAIM_MODEL_DIR)")
    parser.add_argument("--workers", type=int, default=1, help="parallel judge calls (a model judge only)")
    parser.add_argument("--report")
    parser.add_argument("--show-missed", action="store_true")
    args = parser.parse_args(argv)

    judge, judge_model = None, ""
    if args.judge == "oracle":
        judge = "oracle"
    elif args.judge:
        from ..config import get_settings
        from ..llm import build_provider

        settings = get_settings()
        judge, judge_model = build_provider(settings), settings.llm_model_fast
        if judge.name == "mock":
            print("--judge needs a real model (LOOPDESK_LLM_PROVIDER, LOOPDESK_LLM_API_KEY ...)")
            return 2
    report = run(args.mode, args.split.split(","), judge, judge_model, args.classifier, args.model_dir, args.workers)
    for split, r in report["splits"].items():
        s, a, c = r["support"], r["answers"], r["claim"]
        print(f"[{report['mode']}/{report['classifier']}{'+judge:' + report['judge'] if report['judge'] else ''}] {split}: "
              f"claim P {c['precision']} R {c['recall']} F1 {c['f1']} | supported claims failed "
              f"{s.get('false_fail', 0)}/{s.get('supported', 0)} ({s['false_fail_rate']:.1%}) | wrong claims missed "
              f"{s['miss']}/{s.get('unsupported', 0)} | answers ok {a.get('ok_total', 0)}: pass {a.get('ok_pass', 0)} "
              f"fix {a.get('ok_fix', 0)} reask {a.get('ok_reask', 0)} handoff {a.get('ok_handoff', 0)} "
              f"(verdict blocked {a.get('ok_verdict_blocked', 0)}, warn {a.get('ok_verdict_warn', 0)}) | answers bad "
              f"{a.get('bad_total', 0)}: shown {a.get('bad_shown', 0)} | disputed: {r['disputed_answers']}/"
              f"{r['answers_checked']} answers, {r['disputed_claims']}/{r['claims_checked']} claims | judge calls "
              f"{r['judge_calls']} ({r['judge_sentences']} sentences, {r['judge_failed']} without a verdict, p50 "
              f"{r['judge_ms_p50']} ms, p95 {r['judge_ms_p95']} ms) | check p95 {r['check_ms_p95']} ms")
    if "planted" in report:
        p = report["planted"]
        print(f"planted: caught {p['caught']}/{p['total']} " +
              " ".join(f"{k} {v['caught']}/{v['total']}" for k, v in p["by_category"].items()))
        if args.show_missed:
            for m in p["missed"]:
                print("  MISSED", m["category"], m["sentence"][:80], m["checks"])
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
