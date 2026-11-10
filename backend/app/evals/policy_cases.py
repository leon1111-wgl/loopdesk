"""Requests both policy engines answer: cedarpy here, cedar-wasm in the static demo (policy.test.ts).

The cases are generated, not hand-picked: every action, every role, both sides of every limit in the policy, and a
seeded random sample of the rest. Each case stores what the backend decided (effect and the policies that decided it);
the browser's test asks cedar-wasm the same and must get the same answer. ``--write`` regenerates the file after a
policy change; the backend test (tests/test_policy.py) fails if the stored answers no longer match.

    python -m app.evals.policy_cases --write
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
from pathlib import Path
from typing import Any

from ..agent import policy

OUT = Path(__file__).resolve().parents[2] / "evals" / "policy" / "policy_cases.json"
ROLES = ("customer", "agent", "admin")
LIMITS = {"auto_refund_limit": 5000, "approval_admin_threshold": 50000, "return_window_days": 7, "last_refund_day": 15,
          "window_max_count": 3, "window_max_total": 20000}


def _user(role: str, id_: str = "u1") -> dict[str, Any]:
    return policy.entity("User", id_, role=role)


def refund_case(rng: random.Random, edge: bool) -> dict[str, Any]:
    role = rng.choice(ROLES)
    paid = rng.choice((990, 3200, 3990, 5000, 26800, 129900))
    amount = rng.choice((paid, paid - 1, paid + 1, 0, -100, 4999, 5000, 5001, 50000, 50001)) if edge else rng.randint(-500, 140000)
    refund: dict[str, Any] = {
        "amount": amount, "paid": paid, "refundable_status": rng.random() > 0.1, "online_refund": rng.random() > 0.05,
        "awaiting_review": rng.random() < 0.05, "unconfirmed": rng.random() < 0.05, "item_mismatch": rng.random() < 0.05,
        "refunds_in_window": rng.choice((0, 1, 2, 3, 4)), "refunded_in_window": rng.choice((0, 5000, 15000, 19999, 20000)),
        "rejected_recently": rng.random() < 0.1, "customer_has_account": rng.random() > 0.05,
    }
    if rng.random() > 0.2:
        refund["days_since_delivery"] = rng.choice((0, 3, 7, 8, 14, 15, 16, 40))
    if rng.random() < 0.15:
        refund["stated_amount"] = rng.choice((amount, amount + 1, paid, 2000))
    context: dict[str, Any] = {
        "reviewed": rng.random() < 0.25, "customer_declined": rng.random() < 0.05, "customer_asked": rng.random() > 0.1,
        "limits": LIMITS, "reviewers": {"other_admin": rng.random() > 0.2, "other_staff": rng.random() > 0.15},
    }
    if rng.random() > 0.08:  # sometimes the order could not be read: nothing to permit
        context["refund"] = refund
    return {"principal": ["User", "u1"], "action": "request_refund", "resource": ["Order", "SO1"], "context": context,
            "entities": [_user(role)]}


def queue_case(role: str, own: bool, amount: int, approver: str, action: str) -> dict[str, Any]:
    requester = "u1" if own else "u2"
    entities = [_user(role, "u1"), policy.entity("Application", "a1", requester={"__entity": policy.uid("User", requester)},
                                                  amount=amount, approver=approver)]
    if not own:
        entities.append(_user("customer", "u2"))
    return {"principal": ["User", "u1"], "action": action, "resource": ["Application", "a1"],
            "context": {"approval_admin_threshold": 50000}, "entities": entities}


def cases(n_random: int = 600) -> list[dict[str, Any]]:
    rng = random.Random(20261110)
    out = [refund_case(rng, edge=True) for _ in range(n_random)] + [refund_case(rng, edge=False) for _ in range(n_random // 3)]
    for role, own, amount, approver, action in itertools.product(
            ROLES, (True, False), (0, 49999, 50000, 50001, 129900), ("", "agent", "admin"),
            ("approve", "reject", "withdraw_application")):
        out.append(queue_case(role, own, amount, approver, action))
    for role, channel, reviewed, confirmed in itertools.product(ROLES, ("web", "voice", "feishu"), (False, True), (False, True)):
        out.append({"principal": ["User", "u1"], "action": "move_money", "resource": ["Tool", "request_refund"],
                    "context": {"channel": channel, "reviewed": reviewed, "spoken_confirmation": confirmed},
                    "entities": [_user(role), policy.entity("Tool", "request_refund", risk="high")]})
    for role, action, reviewed in itertools.product(
            ROLES, ("withdraw_refund_request", "create_ticket", "handoff_to_human", "call_tenant_tool"), (False, True)):
        out.append({"principal": ["User", "u1"], "action": action, "resource": ["Tool", action],
                    "context": {"reviewed": reviewed}, "entities": [_user(role), policy.entity("Tool", action, risk="write")]})
    return out


def answer(engine: policy.PolicyEngine, case: dict[str, Any]) -> dict[str, Any]:
    d = engine.decide(tuple(case["principal"]), case["action"], tuple(case["resource"]), case["context"], case["entities"])
    return {"effect": d.effect, "reasons": list(d.reasons), "errors": len(d.errors)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the requests both policy engines must answer alike")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    engine = policy.engine()
    rows = [{**case, "expected": answer(engine, case)} for case in cases()]
    effects: dict[str, int] = {}
    for row in rows:
        effects[row["expected"]["effect"]] = effects.get(row["expected"]["effect"], 0) + 1
    print(f"{len(rows)} cases: {effects}")
    if args.write:
        OUT.write_text("[\n" + ",\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n]\n",
                       encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
