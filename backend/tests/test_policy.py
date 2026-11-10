"""The Cedar policy (backend/policies): it validates against its schema, the shared cases keep their answers, and the
properties the release promises hold for the shipped policy and for every variant the demo's what-if panel can make
(any policy without @locked switched off). The browser half is frontend/src/demo/engine/policy.test.ts."""

from __future__ import annotations

import json
import random

import cedarpy
import pytest

from app.agent import policy
from app.evals import policy_cases

TEXT = (policy.POLICY_DIR / "loopdesk.cedar").read_text(encoding="utf-8")
SCHEMA = (policy.POLICY_DIR / "loopdesk.cedarschema").read_text(encoding="utf-8")
SHIPPED = policy.engine()
LOCKED = {pid for pid, notes in SHIPPED.annotations.items() if notes.get("locked") == "true"}
SWITCHABLE = sorted(pid for pid in SHIPPED.policies if pid not in LOCKED)


def variants() -> list[policy.PolicyEngine]:
    """The shipped policy, each switchable policy off on its own, all of them off, and a seeded sample of subsets."""
    rng = random.Random(7)
    subsets = [frozenset(), frozenset(SWITCHABLE)] + [frozenset({pid}) for pid in SWITCHABLE]
    subsets += [frozenset(rng.sample(SWITCHABLE, rng.randint(2, len(SWITCHABLE) - 1))) for _ in range(12)]
    return [policy.PolicyEngine(TEXT, SCHEMA, disabled=s) for s in subsets]


VARIANTS = variants()


def test_the_policy_validates_against_its_schema():
    result = cedarpy.validate_policies(TEXT, SCHEMA)
    assert result.validation_passed, [str(e) for e in result.errors]


def test_every_policy_says_what_it_is():
    for pid, raw in SHIPPED.policies.items():
        kind = pid.split(".", 1)[0]
        assert kind in ("permit", "deny", "review"), pid
        assert (raw["effect"] == "permit") == (kind == "permit"), pid
        if kind != "permit":
            assert SHIPPED.label(pid) != pid, f"{pid} needs a @label for the panel"
    assert LOCKED == {"deny.amount_not_positive", "deny.amount_above_paid", "deny.no_other_reviewer", "deny.four_eyes",
                      "deny.voice_unconfirmed"}


def test_the_engine_is_the_version_the_browser_pins():
    package = json.loads((policy.POLICY_DIR.parents[1] / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert package["dependencies"]["@cedar-policy/cedar-wasm"] == "4.12.0"
    assert "Cedar 4.12.0" in policy.ENGINE


def test_the_shared_cases_keep_their_answers():
    """policy_cases.json is what cedar-wasm must answer too; a policy change regenerates it (--write) on purpose."""
    stored = json.loads(policy_cases.OUT.read_text(encoding="utf-8"))
    assert len(stored) == len(policy_cases.cases())
    wrong = [(case["action"], case["expected"], policy_cases.answer(SHIPPED, case))
             for case in stored if policy_cases.answer(SHIPPED, case) != case["expected"]]
    assert wrong == []


def _refund_contexts(n: int = 400) -> list[dict]:
    rng = random.Random(11)
    out = []
    for _ in range(n):
        case = policy_cases.refund_case(rng, edge=bool(rng.getrandbits(1)))
        if "refund" in case["context"]:
            out.append(case)
    return out


@pytest.mark.parametrize("engine", VARIANTS, ids=lambda e: f"{len(SHIPPED.policies) - len(e.policies)}-off")
def test_no_policy_permits_a_refund_above_what_was_paid(engine):
    for case in _refund_contexts():
        refund = case["context"]["refund"]
        refund["amount"] = refund["paid"] + random.Random(refund["paid"]).choice((1, 100, 5000, 10**7))
        for reviewed in (False, True):
            case["context"]["reviewed"] = reviewed
            decision = engine.decide(("User", "u1"), "request_refund", ("Order", "SO1"), case["context"], case["entities"])
            assert decision.effect == "deny", (decision, refund)  # not allowed, and not sent to a person to allow


@pytest.mark.parametrize("engine", VARIANTS, ids=lambda e: f"{len(SHIPPED.policies) - len(e.policies)}-off")
def test_the_approver_is_never_the_requester(engine):
    for role in policy_cases.ROLES:
        for amount in (0, 100, 49999, 50000, 50001, 10**8):
            for approver in ("", "agent", "admin"):
                for action in ("approve", "reject"):
                    case = policy_cases.queue_case(role, True, amount, approver, action)
                    decision = engine.decide(("User", "u1"), action, ("Application", "a1"), case["context"], case["entities"])
                    assert decision.effect == "deny", (role, amount, approver, action)


@pytest.mark.parametrize("engine", VARIANTS, ids=lambda e: f"{len(SHIPPED.policies) - len(e.policies)}-off")
def test_staff_file_only_what_someone_else_could_approve(engine):
    """Four eyes at filing time: with nobody else of the tier needed, a staff application is refused, not queued."""
    for case in _refund_contexts(200):
        refund = case["context"]["refund"]
        refund["amount"] = min(refund["amount"], refund["paid"])
        case["context"].update(reviewed=False, reviewers={"other_admin": False, "other_staff": False})
        for role in ("agent", "admin"):
            case["entities"] = [policy.entity("User", "u1", role=role)]
            decision = engine.decide(("User", "u1"), "request_refund", ("Order", "SO1"), case["context"], case["entities"])
            assert decision.effect == "deny", (role, decision)


@pytest.mark.parametrize("engine", VARIANTS, ids=lambda e: f"{len(SHIPPED.policies) - len(e.policies)}-off")
def test_money_on_the_voice_channel_needs_the_spoken_yes(engine):
    for role in policy_cases.ROLES:
        decision = engine.decide(("User", "u1"), "move_money", ("Tool", "request_refund"),
                                 {"channel": "voice", "reviewed": False, "spoken_confirmation": False},
                                 [policy.entity("User", "u1", role=role), policy.entity("Tool", "request_refund", risk="high")])
        assert decision.effect == "deny" and decision.reasons == ("deny.voice_unconfirmed",)


def test_deny_by_default():
    """No refund facts (the order could not be read): nothing permits the call, and no forbid needs to say so."""
    decision = SHIPPED.decide(("User", "u1"), "request_refund", ("Order", "SO1"),
                              {"reviewed": False, "customer_declined": False, "customer_asked": True,
                               "limits": policy_cases.LIMITS, "reviewers": {"other_admin": True, "other_staff": True}},
                              [policy.entity("User", "u1", role="customer")])
    assert (decision.effect, decision.reasons) == ("deny", ())
    unknown = SHIPPED.decide(("User", "u1"), "delete_everything", ("Tool", "x"), {}, [policy.entity("User", "u1", role="admin")])
    assert unknown.effect == "deny" and unknown.errors  # an action the schema does not know is refused, with the reason


def test_a_review_needs_a_permit_underneath():
    """review.* forbids alone do not make a call reviewable: without the facts a permit needs, it is denied."""
    decision = SHIPPED.decide(("User", "u1"), "request_refund", ("Order", "SO1"),
                              {"reviewed": False, "customer_declined": False, "customer_asked": True,
                               "limits": policy_cases.LIMITS, "reviewers": {"other_admin": True, "other_staff": True}},
                              [policy.entity("User", "u1", role="agent")])
    assert decision.effect == "deny"
