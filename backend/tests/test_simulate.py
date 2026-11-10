"""Simulated customers graded by their end state (app/evals/simulate.py, backend/evals/simulations)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.db import new_session
from app.evals.simulate import (
    PERSONAS, SIM_DIR, STOP, ModelCustomer, ScriptedCustomer, check_scenario, clean_reply, expected_states, load,
    money_outcome, pass_hat_k, run_conversation, summarize,
)

DEV = load(SIM_DIR / "dev.json")
START = {"orders": {"SO1": "delivered", "SO2": "paid"}}


@pytest.mark.parametrize("scenario", DEV, ids=lambda s: s["key"])
def test_each_dev_goal_can_be_reached_through_the_api(client, scenario):
    """The end state a card asks for is one the real API reaches when the customer says it plainly (the card's sample)."""
    result = run_conversation(client, new_session, scenario, ScriptedCustomer(scenario["sample"]))
    assert result["passed"], (result["outcome"], {t: d for t, d in result["tables"].items() if not d["ok"]})
    assert result["money"] == "ok"


def test_the_dev_set_covers_every_persona_and_both_shops():
    assert {s["persona"] for s in DEV} == set(PERSONAS)
    assert {s["tenant"] for s in DEV} == {"leon", "echo"}
    assert len({s["key"] for s in DEV}) == len(DEV)


def test_what_a_card_leaves_out_must_stay_as_it_was():
    [full] = expected_states({"end": {"refunds": {"SO1": 10}}}, START)
    assert full == {"orders": START["orders"], "refunds": {"SO1": 10.0}, "approvals": {}, "tickets": 0, "handoff": False}


def _actual(refunds=None, approvals=None):
    return {"orders": {}, "refunds": refunds or {}, "approvals": approvals or {}, "tickets": 0, "handoff": False}


def test_money_nobody_asked_for_is_wrong():
    nothing = expected_states({"end": {}}, START)
    review = expected_states({"end": {"approvals": {"pending": 1}}}, START)
    paid = expected_states({"end": {"refunds": {"SO1": 39.9}}}, START)
    assert money_outcome(nothing, _actual(refunds={"SO1": 39.9})) == "wrong"  # paid out on a question
    assert money_outcome(nothing, _actual(approvals={"pending": 1})) == "wrong"  # filed on a question
    assert money_outcome(review, _actual(refunds={"SO2": 268.0})) == "wrong"  # paid what a person had to decide
    assert money_outcome(paid, _actual(refunds={"SO1": 100.0})) == "wrong"  # another amount
    assert money_outcome(paid, _actual(refunds={"SO2": 39.9})) == "wrong"  # another order
    assert money_outcome(paid, _actual()) == "missed"
    assert money_outcome(review, _actual()) == "missed"
    assert money_outcome(paid, _actual(refunds={"SO1": 39.9})) == "ok"
    either = expected_states({"end": {"approvals": {"pending": 1}}, "also_ok": [{}]}, START)
    assert money_outcome(either, _actual()) == "ok"  # nothing filed is also an accepted outcome


def test_the_line_that_ends_the_conversation_is_not_sent(client):
    """τ²-bench's rule: "好的###STOP###" leaves; it does not answer the agent's question with a yes."""
    scenario = next(s for s in DEV if s["key"] == "leon_coffee_cancel")
    result = run_conversation(client, new_session, scenario,
                              ScriptedCustomer(["SO20261012003 还没发货，想取消订单退款", f"好的 {STOP}"]))
    assert result["stop"] == "done"
    assert result["transcript"][-1] == {"role": "customer", "text": "好的", "sent": False}
    assert result["tables"]["approvals"]["actual"] == {}
    assert result["outcome"] == "missed_action"


def test_a_failing_customer_model_ends_the_run_and_says_so(client):
    class Broken:
        def reply(self, scenario, transcript):
            raise httpx.ConnectError("refused")

    result = run_conversation(client, new_session, DEV[0], Broken())
    assert result["stop"].startswith("customer_error: ConnectError")
    assert not result["passed"]


def test_must_say_needs_the_fact_in_an_answer(client):
    scenario = next(s for s in DEV if s["key"] == "leon_robot_where")
    result = run_conversation(client, new_session, scenario, ScriptedCustomer(["你好"]))
    assert result["outcome"] == "not_said" and result["not_said"] == ["AT1402938475"]


def test_the_model_plays_the_customer_with_the_roles_turned_around():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "顾客：“SO20260918001 我要退款”"}}],
                                         "usage": {"prompt_tokens": 300, "completion_tokens": 9},
                                         "timings": {"prompt_per_second": 80.0, "predicted_per_second": 11.5, "prompt_n": 40}})

    customer = ModelCustomer("http://sim", seed=3, transport=httpx.MockTransport(handler))
    transcript = [{"role": "customer", "text": "我想退抱枕"}, {"role": "agent", "text": "请提供订单号"}]
    assert customer.reply(DEV[0], transcript) == "SO20260918001 我要退款"
    roles = [m["role"] for m in seen["messages"]]
    assert roles == ["system", "user", "assistant", "user"]  # greeting, the customer's line, the agent's question
    assert seen["seed"] == 3 and DEV[0]["card"] in seen["messages"][0]["content"]
    assert STOP in seen["messages"][0]["content"]
    assert customer.calls[0]["predicted_per_second"] == 11.5


def test_clean_reply_keeps_only_what_a_customer_would_send():
    assert clean_reply("<think>想想</think>\n用户：好的") == "好的"
    assert clean_reply("「退款吧」") == "退款吧"
    assert clean_reply("customer: refund please") == "refund please"


def test_pass_hat_k_needs_every_seed():
    rows = [{"key": "a", "passed": True}, {"key": "a", "passed": False}, {"key": "b", "passed": True}, {"key": "b", "passed": True}]
    assert pass_hat_k(rows) == (1, 2)
    # a scenario whose other seeds never ran (a job died) is not "passed on every seed" (review P2-3)
    assert pass_hat_k([{"key": "a", "passed": True}, {"key": "b", "passed": True}, {"key": "b", "passed": True}]) == (1, 2)
    assert pass_hat_k([{"key": "a", "passed": True}], k=3) == (0, 1)


def _part(set_name, seed, keys, wrong=0):
    runs = [{"key": k, "seed": seed, "persona": "plain", "passed": True, "outcome": "pass", "handoff": False, "stop": "done",
             "turns": 1, "transcript": []} for k in keys]
    for run in runs[:wrong]:
        run.update(passed=False, outcome="wrong_money")
    return {"meta": {"commit": "abc", "set": set_name, "seeds": [seed], "throughput": {}}, "runs": runs}


def test_merge_refuses_a_report_with_a_seed_missing():
    """A job that died must not leave a smaller, greener report (review P1-2)."""
    from app.evals.simulate import merge

    merged = merge([_part("dev", 1, ["a", "b"]), _part("dev", 2, ["a", "b"])])
    assert merged["summary"]["dev"]["all"]["pass_all_seeds"] == 2 and merged["meta"]["seeds"] == [1, 2]
    with pytest.raises(ValueError, match="not every scenario ran"):
        merge([_part("dev", 1, ["a", "b"]), _part("dev", 2, ["a"])])


def test_stats_counts_copied_lines_and_first_line_filings():
    from app.evals.simulate import stats

    run = {"set": "dev", "key": "leon_pillow_plain", "seed": 1, "transcript": [
        {"role": "customer", "text": "抱枕想退 SO20260918001", "sent": True},
        {"role": "agent", "text": "如需现在申请退款，回复“SO20260918001 申请退款”即可", "tools": [], "approval": False},
        {"role": "customer", "text": "SO20260918001 申请退款", "sent": True},
        {"role": "agent", "text": "已受理", "tools": [{"name": "request_refund", "error": False}], "approval": False},
        {"role": "customer", "text": "谢谢", "sent": False}]}
    assert stats({"meta": {"sets": ["dev"]}, "runs": [run]}) == {
        "customer_lines": 3, "sent": 2, "copied_quoted_line": 1, "money_goal_runs": 1, "filed_on_first_line": 0,
        "filed_later": 1, "never_filed": 0}


def test_summary_counts_wrong_money_per_persona():
    base = {"handoff": False, "stop": "done", "turns": 2}
    rows = [{**base, "key": "a", "persona": "plain", "passed": True, "outcome": "pass"},
            {**base, "key": "b", "persona": "pushy", "passed": False, "outcome": "wrong_money", "handoff": True}]
    summary = summarize(rows)
    assert summary["all"]["wrong_money"] == 1 and summary["by_persona"]["pushy"]["handoffs"] == 1
    assert list(summary["by_persona"]) == ["plain", "pushy"]


def test_a_card_must_name_a_known_persona_and_tables():
    with pytest.raises(ValueError, match="persona"):
        check_scenario({"key": "x", "tenant": "leon", "as": "a", "persona": "angry", "card": "c", "end": {}})
    with pytest.raises(ValueError, match="unknown end-state table"):
        check_scenario({"key": "x", "tenant": "leon", "as": "a", "persona": "plain", "card": "c", "end": {"refund": {}}})


def test_the_second_blind_set_loads_and_carries_no_sample_lines():
    """blind_v2.json (1.2.0) was written by another model from the public brief alone and committed before its first run."""
    blind = load(SIM_DIR / "blind_v2.json")
    assert len(blind) == 12 and not any("sample" in s for s in blind)
    for scenario in blind:
        check_scenario(scenario)


def test_the_blind_set_loads_and_carries_no_sample_lines():
    """blind_v1.json was written by another model from the public rules alone; it is run, never scripted or tuned against."""
    blind = load(SIM_DIR / "blind_v1.json")
    assert len(blind) == 24 and not any("sample" in s for s in blind)
    assert {s["persona"] for s in blind} == set(PERSONAS)
    assert not {s["key"] for s in blind} & {s["key"] for s in DEV}


GRADING = json.loads((SIM_DIR / "grading_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", GRADING["cases"], ids=lambda c: c["name"])
def test_grading_cases_shared_with_the_browser(case):
    from app.evals.simulate import table_diff

    start = GRADING["start"]
    actual = {"orders": {**start["orders"], **case["actual"].get("orders", {})}, "refunds": case["actual"].get("refunds", {}),
              "approvals": case["actual"].get("approvals", {}), "tickets": case["actual"].get("tickets", 0),
              "handoff": case["actual"].get("handoff", False)}
    accepted = expected_states(case["scenario"], start)
    assert money_outcome(accepted, actual) == case["money"]
    passed = any(all(t["ok"] for t in table_diff(e, actual, start["orders"]).values()) for e in accepted)
    assert passed == case["passed"]
