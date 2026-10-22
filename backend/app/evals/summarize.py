"""Summarise repeated eval runs (the real-model workflow, .github/workflows/real-model-eval.yml).

One real-model run is not a measurement: the same configuration varied by six cases out of 52 in
three runs (docs/EVALUATION.md). So every suite runs k times, and this reads the k reports of each
tenant and suite (``<tenant>__<suite>__t<n>.json``, written by ``app.evals.cli --report``) and says:

- the mean and range of passed cases, and pass^k: the share of cases that passed in all k trials;
- which cases always passed, which flipped between trials (flaky), which always failed;
- tokens and the estimated cost per trial (LOOPDESK_PRICES), and the totals for the whole run.

    python -m app.evals.summarize reports/ --json summary.json --markdown summary.md --commit "$GITHUB_SHA"
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

NAME = re.compile(r"(?P<tenant>[\w-]+)__(?P<suite>[\w-]+)__t(?P<trial>\d+)\.json$")


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0


def summarize(directory: Path) -> dict:
    groups: dict[tuple[str, str], list[tuple[int, dict]]] = defaultdict(list)
    for path in sorted(Path(directory).rglob("*.json")):
        match = NAME.search(path.name)
        if match:
            report = json.loads(path.read_text(encoding="utf-8"))
            groups[(match["tenant"], match["suite"])].append((int(match["trial"]), report))
    suites, models = [], set()
    for (tenant, suite), trials in sorted(groups.items()):
        trials.sort(key=lambda t: t[0])
        reports = [r for _, r in trials]
        case_sets = {tuple(sorted(c["key"] for c in r["cases"])) for r in reports}
        if len(case_sets) != 1:
            raise ValueError(f"{tenant}/{suite}: the trials ran different cases; pass^k needs the same ones")
        keys = sorted(case_sets.pop())
        wins = {k: sum(1 for r in reports for c in r["cases"] if c["key"] == k and c["passed"]) for k in keys}
        k = len(reports)
        models.update(r.get("model", "?") for r in reports)
        metrics = [r.get("metrics", {}) for r in reports]
        passed = [r["passed"] for r in reports]
        tokens = [m.get("total_tokens", 0) for m in metrics]
        costs = [m.get("estimated_cost", 0.0) for m in metrics]
        suites.append({
            "tenant": tenant, "suite": suite, "cases": len(keys), "trials": k, "passed": passed,
            "mean_passed": statistics.mean(passed), "min_passed": min(passed), "max_passed": max(passed),
            "pass_hat_k": sum(1 for n in wins.values() if n == k) / len(keys) if keys else 0.0,
            "stable_pass": [key for key, n in wins.items() if n == k],
            "flaky": [key for key, n in wins.items() if 0 < n < k],
            "stable_fail": [key for key, n in wins.items() if n == 0],
            "latency_p50_ms": _median([m.get("latency_p50_ms", 0) for m in metrics]),
            "latency_p95_ms": _median([m.get("latency_p95_ms", 0) for m in metrics]),
            "tokens_per_trial": round(statistics.mean(tokens)),
            "prompt_tokens": sum(m.get("prompt_tokens", 0) for m in metrics),
            "completion_tokens": sum(m.get("completion_tokens", 0) for m in metrics),
            "tokens": sum(tokens),
            "cost_per_trial": statistics.mean(costs),
            "cost": sum(costs),
            "prompt_version": metrics[0].get("prompt_version"),
        })
    return {
        "model": ", ".join(sorted(models)),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "suites": suites,
        "total_tokens": sum(s["tokens"] for s in suites),
        "total_cost": sum(s["cost"] for s in suites),
    }


def markdown(summary: dict, commit: str = "", prices: tuple[float, float] | None = None) -> str:
    model = summary["model"].split(":", 1)[-1]
    lines = [
        f"### Real-model eval: {model}",
        "",
        f"Commit `{commit[:7] or '?'}`, {summary['generated_at'][:10]} (UTC). Synthetic data; each suite ran k times.",
        "",
        "| Tenant | Suite | Cases | Trials | Passed, mean (range) | pass^k | Stable pass / flaky / stable fail "
        "| p50 / p95 s | Tokens per trial | Est. cost per trial |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summary["suites"]:
        always = len(s["stable_pass"])
        lines.append(
            f"| {s['tenant']} | {s['suite']} | {s['cases']} | {s['trials']} "
            f"| {s['mean_passed']:.2f} ({s['min_passed']}–{s['max_passed']}) | {always}/{s['cases']} "
            f"| {always} / {len(s['flaky'])} / {len(s['stable_fail'])} "
            f"| {s['latency_p50_ms'] / 1000:.1f} / {s['latency_p95_ms'] / 1000:.1f} "
            f"| {s['tokens_per_trial']:,} | ¥{s['cost_per_trial']:.3f} |")
    price = f" at ¥{prices[0]} / ¥{prices[1]} per 1M input / output tokens" if prices else ""
    lines += ["", f"Whole run: {summary['total_tokens']:,} tokens, estimated ¥{summary['total_cost']:.2f}{price} "
              "(list price, cache misses; the gateway's own bill may differ).", ""]
    for s in summary["suites"]:
        if s["flaky"] or s["stable_fail"]:
            lines.append(f"- {s['tenant']}/{s['suite']}: flaky: {', '.join(s['flaky']) or 'none'}; "
                         f"always failed: {', '.join(s['stable_fail']) or 'none'}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise repeated eval reports")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--commit", default="")
    parser.add_argument("--prices", help="input,output CNY per 1M tokens, as used for the estimate")
    args = parser.parse_args(argv)
    summary = summarize(args.directory)
    if not summary["suites"]:
        print(f"no <tenant>__<suite>__t<n>.json reports under {args.directory}")
        return 1
    prices = tuple(float(p) for p in args.prices.split(",")) if args.prices else None
    summary["commit"], summary["prices_cny_per_mtok"] = args.commit, prices
    text = markdown(summary, args.commit, prices)
    if args.json:
        args.json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
