"""Every decision the tool gate makes on the parity sets, recorded so two versions of the rules can be compared.

The policy rewrite (Cedar, ``backend/policies``) was held to this: the decisions were recorded on the code before it
(``backend/evals/policy/decisions_backend.json``) and every later version must record the same lines, or the change in
them is a decision someone made and wrote down. A line is one tool call or one reviewer's decision, as the caller saw it
(allowed, sent to a person, refused with which words, answered with a question); ids and clock times are placeholders.

The sets:
- ``journeys``: the 15 whole conversations of ``evals/journeys.json``, reviewers' decisions included;
- ``samples``: the sample lines of the 24 dev cards (``evals/simulations/dev.json``);
- ``transcripts``: the customer lines of the 144 recorded simulated conversations (``demo/simulations/report.json``,
  all 48 cards, 3 seeds), replayed one conversation per fresh database;
- ``money``: the money probes as ``app.evals.money_rule --whole`` runs them (the review's 331 / 274 / 205 lists and the
  five money sets, plus the main session's probe of 1.1.0, ``evals/policy/probe_r2_main.json``), dry runs on one
  database.

    python -m app.evals.decisions --record ../backend/evals/policy/decisions_backend.json
    python -m app.evals.decisions --check evals/policy/decisions_backend.json   # exit 1 on any difference
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..agent import decision_log
from . import journeys, money_rule, simulate

ROOT = Path(__file__).resolve().parents[3]
REPORT = ROOT / "demo" / "simulations" / "report.json"
SETS = ("journeys", "samples", "transcripts", "money")


def recorded_conversations() -> list[dict[str, Any]]:
    """The recorded runs as replayable cards: the customer lines that were sent, in order."""
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    cards = {c["key"]: c for name in ("dev.json", "blind_v1.json") for c in simulate.load(simulate.SIM_DIR / name)}
    out = []
    for run in report["runs"]:
        lines = [t["text"] for t in run["transcript"] if t["role"] == "customer" and t.get("sent")]
        out.append({"key": f"{run['set']}/{run['key']}/seed{run['seed']}", "card": cards[run["key"]], "lines": lines})
    return out


@contextlib.contextmanager
def _fresh() -> Iterator[tuple[Any, Any]]:
    """A fresh backend with the process-wide caches emptied, as the test fixture does: the chat rate limit (30 a
    minute per user) would otherwise refuse turns depending on how fast the cases run, and the recording would differ."""
    from ..rag.retrieval import index_manager
    from ..security import rate_limiter

    rate_limiter._hits.clear()
    index_manager._cache.clear()
    index_manager._tokens.clear()
    with simulate.fresh_backend() as pair:
        yield pair


def _journeys() -> dict[str, list[dict[str, Any]]]:
    out = {}
    for journey in journeys.load():
        with _fresh() as (client, new_session), decision_log.recording() as records:
            journeys.run_journey(client, new_session, journey)
        out[journey["key"]] = records
    return out


def _scripted(cases: list[tuple[str, dict[str, Any], list[str]]]) -> dict[str, list[dict[str, Any]]]:
    out = {}
    for key, card, lines in cases:
        with _fresh() as (client, new_session), decision_log.recording() as records:
            simulate.run_conversation(client, new_session, card, simulate.ScriptedCustomer(lines))
        out[key] = records
    return out


def _money() -> dict[str, list[dict[str, Any]]]:
    backend, lists, out = money_rule.Backend(), money_rule.review_lists(), {}
    try:
        def run(key: str, tenant: str, email: str, turns: list[str]) -> None:
            with decision_log.recording() as records:
                backend.run(tenant, email, turns)
            out[key] = records

        for name in ("adversarial", "plain"):
            for i, text in enumerate(lists[name]):
                run(f"{name}/{i}", "leon", "customer@leon.demo", [text])
        for i, case in enumerate(lists["multi_turn"]):
            run(f"multi_turn/{i}", case["email"].split("@")[1].split(".")[0], case["email"], case["turns"])
        for name in (*money_rule.SETS, money_rule.FRESH_SET):
            for i, case in enumerate(money_rule.gate_cases(name)):
                email = case.get("as_user") or f"customer@{case['tenant']}.demo"
                run(f"{name}/{i}", case["tenant"], email, [*case.get("history", []), case["question"]])
        probes = json.loads((money_rule.EVAL_DIR / "policy" / "probe_r2_main.json").read_text(encoding="utf-8"))
        for i, probe in enumerate(probes["probes"]):
            run(f"probe_r2_main/{i}", "leon", "customer@leon.demo", [probe["line"]])
    finally:
        backend.db.close()
    return out


def record(sets: tuple[str, ...] = SETS) -> dict[str, dict[str, list[dict[str, Any]]]]:
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for name in sets:
        if name == "journeys":
            out[name] = _journeys()
        elif name == "samples":
            dev = simulate.load(simulate.SIM_DIR / "dev.json")
            out[name] = _scripted([(c["key"], c, c["sample"]) for c in dev])
        elif name == "transcripts":
            out[name] = _scripted([(c["key"], c["card"], c["lines"]) for c in recorded_conversations()])
        elif name == "money":
            out[name] = _money()
    return out


def dumps(sets: dict[str, dict[str, list[dict[str, Any]]]]) -> str:
    """One case per line, so a change in the rules shows as a readable diff."""
    lines = []
    for name, cases in sets.items():
        for key, records in cases.items():
            lines.append(json.dumps(f"{name}/{key}", ensure_ascii=False) + ": "
                         + json.dumps(records, ensure_ascii=False, sort_keys=True))
    return "{\n" + ",\n".join(lines) + "\n}\n"


def counts(flat: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    effects = Counter(r["effect"] for records in flat.values() for r in records)
    return {"cases": len(flat), "decisions": sum(len(r) for r in flat.values()), "by_effect": dict(sorted(effects.items()))}


def flatten(sets: dict[str, dict[str, list[dict[str, Any]]]]) -> dict[str, list[dict[str, Any]]]:
    return {f"{name}/{key}": records for name, cases in sets.items() for key, records in cases.items()}


def differences(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    keys = sorted(set(expected) | set(actual))
    return [key for key in keys if expected.get(key) != actual.get(key)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record or check every tool-gate decision on the parity sets")
    parser.add_argument("--record", type=Path, help="write the decisions here")
    parser.add_argument("--check", type=Path, help="compare with a recording; exit 1 on any difference")
    parser.add_argument("--sets", default=",".join(SETS))
    args = parser.parse_args(argv)
    sets = tuple(s for s in args.sets.split(",") if s)
    if unknown := [s for s in sets if s not in SETS]:
        parser.error(f"unknown sets: {unknown}")
    found = record(sets)
    flat = flatten(found)
    print(f"recorded: {json.dumps(counts(flat), ensure_ascii=False)}")
    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(dumps(found), encoding="utf-8")
    if args.check:
        expected = {k: v for k, v in json.loads(args.check.read_text(encoding="utf-8")).items()
                    if k.split("/", 1)[0] in sets}
        print(f"expected: {json.dumps(counts(expected), ensure_ascii=False)}")
        diff = differences(expected, flat)
        for key in diff[:40]:
            print(f"DIFF {key}\n  expected {json.dumps(expected.get(key), ensure_ascii=False)[:600]}"
                  f"\n  actual   {json.dumps(flat.get(key), ensure_ascii=False)[:600]}")
        print(f"{len(diff)} of {len(set(expected) | set(flat))} cases differ")
        return 1 if diff else 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
