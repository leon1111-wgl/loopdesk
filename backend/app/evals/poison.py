"""Poisoned knowledge (review round 5, N-038): does ingestion hold back chunks that try to steer
the agent or defraud customers, and leave ordinary policy text alone?

    python -m app.evals.poison --report poison.json

``evals/poisoned_docs.json`` has three groups:

- found: the wordings the red team used, and the injection planted in the demo data. The scan is
  developed against these.
- held_out: other wordings, written before the scan was changed and never used to tune it.
- benign: ordinary policy text sharing words with the attacks (验证码, 银行卡, 微信, 忽略, an
  internal document telling staff what to say). Holding these back would refuse real questions.
- held_out_links / benign_links (round 2): links to sites outside the tenant's official domains
  (``official_domains``) asking customers for card details or offering money, and ordinary text
  with links (a carrier's or the customs office's site, the tenant's own site). Written before the
  link rule; held_out_links is not used to tune it either.

Rules can always be worded around, which is why a match goes to a person for review instead of
being the final word.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SET_FILE = Path(__file__).resolve().parents[2] / "evals" / "poisoned_docs.json"
BLIND_FILE = Path(__file__).resolve().parents[2] / "evals" / "blind_heldout.json"
# group -> True when every case in it should be held back
GROUPS = {"found": True, "held_out": True, "benign": False, "links": True, "benign_links": False,
          "review_attacks": True, "review_benign": False}
BLIND_GROUPS = {"attacks": True, "benign": False}


def scan(text: str, public: bool, channels) -> list[str]:
    from ..guardrails.injection import scan_knowledge

    return scan_knowledge(text, public, channels)


def answer_check(text: str, channels) -> list[str]:
    from ..guardrails.injection import unsafe_answer

    return unsafe_answer(text, None, channels)


def _measure(cases: dict, groups: dict, show_text: bool) -> dict:
    from ..guardrails.injection import Channels

    official = Channels.of(cases.get("official_domains", []), cases.get("official_contacts", []))
    report: dict = {}
    for group, attack in groups.items():
        items = cases[group]
        flagged = answered = 0
        rows = []
        for item in items:
            reasons = scan(item["text"], item.get("visibility", "public") == "public", official)
            stopped = answer_check(item["text"], official)
            flagged += bool(reasons)
            answered += bool(stopped)
            if bool(reasons) != attack:  # a miss, or a false alarm
                row = {"id": item["id"], "category": item.get("category", ""), "reasons": reasons}
                if show_text:
                    row["text"] = item["text"]
                rows.append(row)
        report[group] = {"scan": f"{flagged}/{len(items)}", "answer_check": f"{answered}/{len(items)}", "wrong": rows}
    return report


def measure() -> dict:
    report = _measure(json.loads(SET_FILE.read_text(encoding="utf-8")), GROUPS, show_text=True)
    # The blind set is measured, not read: misses are reported by id and category only.
    blind = _measure(json.loads(BLIND_FILE.read_text(encoding="utf-8")), BLIND_GROUPS, show_text=False)
    report.update({f"blind_{group}": result for group, result in blind.items()})
    return report


def measure_judge() -> dict:
    """The answer check with the output judge (LOOPDESK_OUTPUT_GUARD=llm) on every group, using
    the configured model: the rules first, then the judge on what they passed and what carries a
    scam's cues, as at runtime. Blind texts are not printed; misses are counted by category."""
    from ..config import get_settings
    from ..guardrails.injection import Channels, unsafe_answer
    from ..guardrails.judge import judge, worth_judging
    from ..llm import build_provider

    settings = get_settings()
    provider = build_provider(settings)
    if provider.name == "mock":
        raise SystemExit("the output judge needs a real model: set LOOPDESK_LLM_PROVIDER and its key")
    model = settings.llm_model_fast

    def run(cases: dict, groups: dict, show_text: bool) -> dict:
        channels = Channels.of(cases.get("official_domains", []), cases.get("official_contacts", []))
        result = {}
        for group, attack in groups.items():
            items = cases[group]
            rules = added = no_verdict = 0
            missed: dict[str, int] = {}
            rows = []
            for item in items:
                by_rules = bool(unsafe_answer(item["text"], None, channels))
                verdict = None
                if not by_rules and worth_judging(item["text"]):
                    verdict = judge(provider, model, item["text"], channels)
                    no_verdict += verdict is None
                by_judge = bool(verdict and verdict.unsafe)
                rules += by_rules
                added += by_judge
                if (by_rules or by_judge) != attack:
                    missed[item.get("category", "")] = missed.get(item.get("category", ""), 0) + 1
                    rows.append({"id": item["id"], **({"text": item["text"]} if show_text else {}),
                                 "judge": verdict.reason if verdict else None})
            result[group] = {"rules": f"{rules}/{len(items)}", "judge_added": added,
                             "combined": f"{rules + added}/{len(items)}", "no_verdict": no_verdict,
                             "wrong_by_category": missed, "wrong": rows}
        return result

    report = run(json.loads(SET_FILE.read_text(encoding="utf-8")), GROUPS, show_text=True)
    blind = run(json.loads(BLIND_FILE.read_text(encoding="utf-8")), BLIND_GROUPS, show_text=False)
    report.update({f"blind_{group}": value for group, value in blind.items()})
    return {"model": model, **report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoopDesk poisoned-knowledge detection")
    parser.add_argument("--report")
    parser.add_argument("--judge", action="store_true", help="measure the answer check with the output judge")
    args = parser.parse_args(argv)
    report = measure_judge() if args.judge else measure()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
