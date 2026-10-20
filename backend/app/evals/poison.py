"""Poisoned knowledge (review round 5, N-038): does ingestion hold back chunks that try to steer
the agent or defraud customers, and leave ordinary policy text alone?

    python -m app.evals.poison --report poison.json

``evals/poisoned_docs.json`` has three groups:

- found: the wordings the red team used, and the injection planted in the demo data. The scan is
  developed against these.
- held_out: other wordings, written before the scan was changed and never used to tune it.
- benign: ordinary policy text sharing words with the attacks (验证码, 银行卡, 微信, 忽略, an
  internal document telling staff what to say). Holding these back would refuse real questions.

Ingestion is only the first layer; what a chunk that gets through can make the agent say is the
output check's job. Read the numbers with that in mind: a miss here is not a phishing answer.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SET_FILE = Path(__file__).resolve().parents[2] / "evals" / "poisoned_docs.json"


def scan(text: str, public: bool) -> list[str]:
    from ..guardrails.injection import detect_injection

    return detect_injection(text).matches


def measure() -> dict:
    cases = json.loads(SET_FILE.read_text(encoding="utf-8"))
    report: dict = {}
    for group in ("found", "held_out", "benign"):
        items = cases[group]
        flagged, rows = 0, []
        for item in items:
            reasons = scan(item["text"], item.get("visibility", "public") == "public")
            flagged += bool(reasons)
            if bool(reasons) != (group != "benign"):  # a miss, or a false alarm
                rows.append({"id": item["id"], "text": item["text"], "reasons": reasons})
        report[group] = {"flagged": f"{flagged}/{len(items)}", "wrong": rows}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoopDesk poisoned-knowledge detection")
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    report = measure()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
