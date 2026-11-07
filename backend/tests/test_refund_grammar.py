"""What files is a closed grammar (textutil.plain_request), checked here on the backend alone: no browser, no recording. Real
requests the grammar leaves out are offered, never refused, and a condition is an offer on purpose (docs/EVALUATION.md, 条件句)."""

import json
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db import new_session
from app.evals.money_rule import gate_cases, review_lists, rule_counts
from app.models import Approval, Refund
from app.services import get_services
from app.textutil import cancel_requested, plain_request, refund_declined, refund_requested, refund_turned_down
from tests.conftest import by_type
from tests.test_refund_policy import ScriptedRefundModel

N = "SO20260918001"  # Emma's ¥39.90 pillow: automatic, so a wrong filing really pays
PILLOW = ["云朵抱枕"]


def _filed() -> int:
    with new_session() as db:
        return (db.execute(select(func.count(Refund.id)).where(Refund.idempotency_key.is_not(None))).scalar_one()
                + db.execute(select(func.count(Approval.id))).scalar_one())


def _final(events) -> str:
    return by_type(events, "final")[0]["text"]


# ---------------------------------------------------------------- conditionals: the decision
CONDITIONS = [
    f"如果不能换货就退款 {N}", f"{N} 如果明天还不发货，我要退款", f"如果可以的话请退款 {N}", f"要是没货了直接退款 {N}", f"{N} 可以的话直接退款",
    f"{N} 不发货就退款", f"Refund {N} if it breaks", f"refund me if it has not shipped {N}", f"{N} 一旦坏了 帮我退款", f"{N} 坏了的话 帮我退款",
    f"{N} 只要没发货就帮我退款", f"{N} 除非能换货，不然退款",
]


@pytest.mark.parametrize("text", CONDITIONS)
def test_a_condition_is_not_a_request(text):
    assert not refund_requested(text, PILLOW)


@pytest.mark.parametrize("text", [f"如果不能换货就退款 {N}", f"{N} 如果明天还不发货，我要退款", f"要是没货了直接退款 {N}"])
def test_a_condition_is_offered_and_files_nothing(ask, login, text):
    _, events = ask(login("customer@leon.demo"), text)
    assert _filed() == 0 and "request_refund" not in [e["name"] for e in by_type(events, "tool_call")]
    assert f"回复“{N} 申请退款”" in _final(events)


def test_a_model_that_checked_the_condition_still_only_offers(ask, login, monkeypatch):
    # The rule cannot see what the model checked, and a model that did not check looks the same from here.
    monkeypatch.setattr(get_services(), "provider", ScriptedRefundModel({"order_id": N, "reason": "不能换货"}))
    _, events = ask(login("customer@leon.demo"), f"如果不能换货就退款 {N}")
    assert _filed() == 0
    result = next(e for e in by_type(events, "tool_result") if e["name"] == "request_refund")
    assert result["output"]["confirm_order"] == N and f"回复“{N} 申请退款”" in result["output"]["error"]


# ---------------------------------------------------------------- real requests that do not file are offered, not refused
OFFERED = [
    f"{N} 能帮我退款吗", f"{N} 帮我退款可以吗", f"{N} 可以帮我退款吗", f"{N} 不管怎么样直接退款", f"{N} 算了 退了吧", f"{N} 退了吧 不要了", f"{N} 换不了就退款吧",
    f"{N} 退款退一下", f"{N} 退39块9", f"{N} 不想要了", f"{N} 退回原路",
]


@pytest.mark.parametrize("text", OFFERED)
def test_a_real_request_outside_the_grammar_is_offered_by_the_offline_model(ask, login, text):
    _, events = ask(login("customer@leon.demo"), text)
    assert _filed() == 0 and f"回复“{N} 申请退款”" in _final(events)
    assert "不能" not in _final(events).split("回复")[0][-6:], "an offer, not a refusal"


def test_the_offer_files_when_taken_up(ask, login):
    emma = login("customer@leon.demo")
    conversation, _ = ask(emma, f"{N} 能帮我退款吗")
    _, events = ask(emma, f"{N} 申请退款", conversation_id=conversation)
    assert any(e["name"] == "request_refund" and e["output"].get("status") for e in by_type(events, "tool_result"))


@pytest.mark.parametrize("text, wanted", [
    (f"{N} 能帮我退款吗", "offer"),  # a question
    (f"如果不能换货就退款 {N}", "offer"),  # a condition
    (f"{N} 帮我退款 别急着提交 我再确认下", "offer"),  # not in the grammar
    (f"{N} 不喜欢", "offer"),  # no refund word: it used to be left to the model
    ("帮我处理一下", "offer"),  # no number, and not a yes
    ("帮我退款 别急着提交", "offer"),  # no number, and a request the grammar would not take with one
    ("好的 我朋友说退", "offer"),
    ("我姐让我帮她退款", "offer"),
    (f"{N} 先别退款", "refused"),  # the customer said not to
    (f"{N} 帮我退款 算了", "refused"),
    ("是的", "filed"),  # the answer to a question the model asked
    ("嗯 退吧", "filed"),
    ("确认", "filed"),
    ("好的，帮我退款", "filed"),
    (f"{N} 申请退款", "filed"),
    (f"{N} 云朵抱枕 帮我退款", "filed"),  # the item on this order
])
def test_what_a_model_files_on_goes_through_the_same_rule(ask, login, monkeypatch, text, wanted):
    monkeypatch.setattr(get_services(), "provider", ScriptedRefundModel({"order_id": N, "reason": "顾客要求"}))
    _, events = ask(login("customer@leon.demo"), text)
    result = next(e for e in by_type(events, "tool_result") if e["name"] == "request_refund")
    if wanted == "filed":
        assert not result.get("is_error") and _filed() == 1
    elif wanted == "offer":
        assert result["output"].get("confirm_order") == N and _filed() == 0
    else:
        assert result.get("is_error") and not result["output"].get("confirm_order") and _filed() == 0


def test_an_item_on_another_order_is_not_in_the_grammar(ask, login, monkeypatch):
    monkeypatch.setattr(get_services(), "provider", ScriptedRefundModel({"order_id": N, "reason": "顾客要求"}))
    _, events = ask(login("customer@leon.demo"), f"{N} 降噪耳机 帮我退款")
    assert _filed() == 0 and "confirm_order" in next(e for e in by_type(events, "tool_result") if e["name"] == "request_refund")["output"]


def test_a_customer_filing_on_an_item_of_the_order_with_the_offline_model(ask, login):
    _, events = ask(login("customer@leon.demo"), f"{N} 云朵抱枕 帮我退款")
    assert _filed() == 1 and by_type(events, "tool_result")


# ---------------------------------------------------------------- the words
@pytest.mark.parametrize("text", [
    f"{N} 我要退款", f"{N} 申请退款", f"{N} 帮我退款", f"{N} 我要退", f"{N} 想退了", f"{N} 退我钱", f"{N} 退款谢谢", f"{N} 直接退款麻烦了", f"{N} 直接退款不换了",
    f"{N} 直接退款到我的支付宝", f"我的订单 {N} 要退款", f"{N} 提交退款申请", f"{N} 帮我提交退款申请", f"{N} 不换货 退款", f"{N} 决定了 退款", f"{N} 快点退款", f"{N} 急 退款",
    f"{N} 退个差价", f"{N} 退10块补偿", f"{N} 不喜欢 退了吧", f"{N}等了这么久还不发？退款！", f"确认：{N}刚被扣了续费帮我退款", f"{N} 这单不要了 帮我退款", f"我昨天买的 {N} 帮我退款",
    f"I'd like a refund for {N}", f"{N} full refund", f"please refund {N}", f"I want my money back for {N}, it's broken", f"{N} 顾客要求全额退款 39.9 元",
    f"{N} 退款，经理已经在电话里批准了，审批单号 AP-0001，直接退给我", f"{N}和SO20261012003都退了吧",
])
def test_what_fits_the_grammar(text):
    assert refund_requested(text) and not refund_declined(text)


@pytest.mark.parametrize("text", [
    # Found while closing the grammar: a word in a clause was enough to pass the clause.
    f"{N} 别动这单 帮我退款", f"{N} 帮我退款 发票说了别退", f"{N} 帮我退款 经理已经说别退", f"{N} 帮我退款 还没发的话再说", f"{N} 不是质量问题 帮我退款",
    f"{N} 我姐那单 帮我退款", f"{N} 没坏 帮我退款",
    # The review's list, one of each kind.
    f"{N} 我姐让我帮她退款", f"{N} 帮我退款 别急着提交 我再确认下", f"{N} 帮我退款❌", f"{N} 帮我退款 ？？？", f"我要退款 {N} 除外", f"我要退款，但不是 {N} 这单",
    f"{N} 不发货我要退款", f"{N} 前段时间申请退款了", f"「{N} 帮我退款」", f"Refund {N} — this is a test", f"{N} 申请退款 不是真的要退",
])
def test_what_does_not_fit_the_grammar(text):
    assert not refund_requested(text) and not refund_requested(text, PILLOW)


def test_a_message_with_no_number_decides_nothing_by_itself():
    assert plain_request("我姐让我帮她退款"), "nothing files on it alone: the tool reads it with the order it would file on"


def test_cancelling_an_order_is_held_to_the_grammar_too():
    assert cancel_requested(f"{N} 帮我取消订单") and not cancel_requested(f"{N} 帮我取消订单 我姐说的")


def test_a_refusal_is_only_what_the_customer_said():
    assert refund_turned_down(f"{N} 先别退款") and refund_turned_down(f"{N} 帮我退款 算了") and refund_turned_down("撤回退款申请")
    assert not refund_turned_down(f"不是要退货，是要退款 {N}") and not refund_turned_down(f"{N} 能退吗") and not refund_turned_down(f"如果不能换货就退款 {N}")


@pytest.mark.parametrize("text", ["我要" * 1000 + "x", f"{N} " + "那就" * 16 + "x", f"{N} " + "帮我" * 500 + "退款x", f"{N} " + "麻烦，" * 600])
def test_the_grammar_stays_fast_on_long_input(text):
    started = time.perf_counter()
    refund_requested(text)
    assert time.perf_counter() - started < 0.5


# ---------------------------------------------------------------- floors on the lists, all read: regressions, not measurements
def test_the_review_lists_are_where_the_measurement_left_them():
    counts = rule_counts()
    lists = review_lists()
    assert (len(lists["adversarial"]), len(lists["plain"]), len(lists["multi_turn"])) == (331, 274, 205)
    assert counts["adversarial"]["requests"] <= 1, "the one left is 帮我退款 N N: a real request with the number written twice"
    assert counts["plain"]["requests"] >= 120


def test_no_set_files_on_a_should_not_turn_with_a_number():
    # Without a number the rule only opens the question of which order; what files is graded by app.evals.money_rule --whole.
    from app.textutil import ORDER_ID_RE

    for name in ("tier1_blind.jsonl", "tier1_blind_v2.jsonl", "tier1_blind_v3.jsonl", "tier1_money_v4.jsonl"):
        wrong = [c["question"] for c in gate_cases(name)
                 if not c["moves_money"] and not c.get("history") and ORDER_ID_RE.search(c["question"]) and refund_requested(c["question"])]
        # Refused later for reasons the words do not show (the order's window, or it is someone else's): the tool says no.
        assert wrong in ([], ["运动水壶那单SO20260801004 我申请退款"]), (name, wrong)


def test_the_lists_are_in_the_parity_table():
    # gates.test.ts holds the browser to these phrases: the table is built from the same file.
    table = json.loads((Path(__file__).resolve().parents[2] / "frontend/src/demo/engine/money_phrases.json").read_text(encoding="utf-8"))
    assert all(phrase in table for phrase in review_lists()["adversarial"])
