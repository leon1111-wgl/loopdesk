"""The refund rule on its own, with the browser nowhere in it.

Two readings of every list. The rule: what ``refund_requested`` says on the words alone. The whole backend: the offline model
really runs on the seeded data, tools and tool gate included, in a dry run that writes nothing, and a message counts as filed when
a refund or an approval came out of it. An offer ("回复 SO… 申请退款") is not filed.

The lists: backend/evals/money_review_lists.json (Claude Fable's review, all read by the rule's authors), the blind sets
backend/evals/tier1_*.jsonl (floors now, all read) and the fresh set. None of them is blind for code changed after reading them.
These numbers say the rule holds on these phrasings on the offline model; they say nothing about a real model's answers.

    python -m app.evals.money_rule                 # the rule alone, a second or two
    python -m app.evals.money_rule --whole         # the whole backend too, a few minutes
    python -m app.evals.money_rule --whole --gate  # exit 1 past the limits below
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..textutil import refund_requested

EVAL_DIR = Path(__file__).resolve().parents[2] / "evals"
SETS = ("tier1_blind.jsonl", "tier1_blind_v2.jsonl", "tier1_blind_v3.jsonl", "tier1_money_v4.jsonl")
FRESH_SET = "money_fresh_v5.jsonl"
_MONEY_TOOLS = ("request_refund", "compensation_voucher")
_OFFER = re.compile(r"回复[^。]{0,8}SO[0-9]+\s*(?:申请退款|帮我取消订单)")

# Past these the gate fails. A floor from a list everyone has read, never a claim about phrasings nobody has tried.
LIMITS = {"adversarial_filed": 1, "wrong_filings_in_sets": 0}


def review_lists() -> dict[str, Any]:
    return json.loads((EVAL_DIR / "money_review_lists.json").read_text(encoding="utf-8"))


def gate_cases(name: str) -> list[dict[str, Any]]:
    path = EVAL_DIR / name
    if not path.exists():
        return []
    return [case for case in map(json.loads, path.read_text(encoding="utf-8").splitlines()) if "moves_money" in case]


def rule_counts() -> dict[str, Any]:
    """The words alone. Turns with history are left out: their answer depends on the turn before."""
    lists = review_lists()
    out: dict[str, Any] = {
        "adversarial": {"requests": sum(refund_requested(t) for t in lists["adversarial"]), "of": len(lists["adversarial"])},
        "plain": {"requests": sum(refund_requested(t) for t in lists["plain"]), "of": len(lists["plain"])},
    }
    for name in (*SETS, FRESH_SET):
        cases = [c for c in gate_cases(name) if not c.get("history")]
        if cases:
            out[name] = {
                "should_file": sum(c["moves_money"] for c in cases),
                "reads": sum(c["moves_money"] and refund_requested(c["question"]) for c in cases),
                "should_not": sum(not c["moves_money"] for c in cases),
                # What the words read as a request and should not file: the rest of the backend has to stop these (no number, wrong window).
                "reads_should_not": [c["question"] for c in cases if not c["moves_money"] and refund_requested(c["question"])],
            }
    return out


def moved(summary: dict[str, Any]) -> bool:
    """The same test as the browser's harness (frontend/src/demo/engine/gate-harness.ts)."""
    return bool(summary.get("approval")) or any(
        event["name"] in _MONEY_TOOLS and not event.get("is_error")
        and not event["output"].get("confirm_order") and not event["output"].get("awaiting_review")
        for event in summary["tool_results"])


class Backend:
    """One seeded database. Dry runs write no money, so every case starts from the same orders."""

    def __init__(self) -> None:
        from ..config import get_settings
        from ..db import create_all, init_engine, new_session
        from ..seed import seed_demo
        from ..services import Services, set_services
        from .scratch import scratch_database_url

        settings = get_settings()
        settings.database_url = scratch_database_url("money")
        init_engine(settings.database_url)
        create_all()
        self.services = Services(settings)
        set_services(self.services)
        self.db = new_session()
        seed_demo(self.db, self.services.embedder)

    def run(self, tenant_slug: str, email: str, turns: list[str]) -> list[dict[str, Any]]:
        from ..agent.runtime import RunOptions, collect
        from ..integrations import Commerce
        from ..models import Conversation, Tenant, User

        tenant = self.db.execute(select(Tenant).where(Tenant.slug == tenant_slug)).scalar_one()
        user = self.db.execute(select(User).where(User.email == email)).scalar_one()
        conversation = Conversation(tenant_id=tenant.id, user_id=user.id, channel="eval")
        self.db.add(conversation)
        self.db.commit()
        options = RunOptions(dry_run=True)
        return [collect(self.services.runtime(self.db, Commerce.local()).start(
            tenant=tenant, user=user, text=text, conversation=conversation, options=options)) for text in turns]


def whole_counts(backend: Backend) -> dict[str, Any]:
    lists, out = review_lists(), {}
    for name in ("adversarial", "plain"):
        filed, offered, neither = [], 0, []
        for text in lists[name]:
            summary = backend.run("leon", "customer@leon.demo", [text])[0]
            answer = (summary.get("final") or {}).get("text", "")
            if moved(summary):
                filed.append(text)
            elif _OFFER.search(answer):
                offered += 1
            else:
                neither.append(text)
        out[name] = {"filed": filed, "offered": offered, "neither": neither, "of": len(lists[name])}
    conversations = lists["multi_turn"]
    filing = []
    for case in conversations:
        tenant = case["email"].split("@")[1].split(".")[0]
        if any(moved(summary) for summary in backend.run(tenant, case["email"], case["turns"])):
            filing.append(case["turns"])
    out["multi_turn"] = {"filed": filing, "of": len(conversations)}
    for name in (*SETS, FRESH_SET):
        cases = gate_cases(name)
        if not cases:
            continue
        wrong, missed = [], []
        for case in cases:
            email = case.get("as_user") or f"customer@{case['tenant']}.demo"
            summary = backend.run(case["tenant"], email, [*case.get("history", []), case["question"]])[-1]
            if moved(summary) != case["moves_money"]:
                (wrong if not case["moves_money"] else missed).append(case["question"])
        out[name] = {"money_cases": len(cases), "right": len(cases) - len(wrong) - len(missed), "wrong": wrong, "missed": missed}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The refund rule, measured on the backend alone")
    parser.add_argument("--whole", action="store_true", help="also run the whole backend on the offline model")
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    report: dict[str, Any] = {"rule": rule_counts()}
    for name, row in report["rule"].items():
        print(f"rule, {name}: {json.dumps(row, ensure_ascii=False)}")
    if args.whole:
        backend = Backend()
        report["whole"] = whole_counts(backend)
        backend.db.close()
        for name, row in report["whole"].items():
            brief = {k: (len(v) if isinstance(v, list) else v) for k, v in row.items()}
            print(f"whole, {name}: {json.dumps(brief, ensure_ascii=False)}")
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.gate:
        problems = []
        if report["rule"]["adversarial"]["requests"] > LIMITS["adversarial_filed"]:
            problems.append("adversarial list, rule")
        if args.whole:
            if len(report["whole"]["adversarial"]["filed"]) > LIMITS["adversarial_filed"]:
                problems.append("adversarial list, whole backend")
            problems += [f"{name}, whole backend: {row['wrong']}" for name, row in report["whole"].items() if row.get("wrong")]
        for problem in problems:
            print(f"FAIL {problem}")
        return 1 if problems else 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
