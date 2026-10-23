"""A follow-up keeps the order the conversation is about, and an amount is a refund (N-155).

Jack wrote "SO20261003005 耳机有杂音", then "嗯 退款 100 元": the second turn searched the knowledge base and
answered "100积分可抵扣1元…" (verdict pass). "退 100 块补偿就行" and "退 9999 元" only got the order status."""

import pytest
from sqlalchemy import select

from app.agent.rewrite import carry_order
from app.db import new_session
from app.models import Approval, Message
from app.textutil import refund_requested
from tests.conftest import by_type

JACK_ORDER = "SO20261003005"  # Jack's ¥899 headphones, delivered 14 days ago: refunds go to review
EMMA_ORDER = "SO20261008010"  # Emma's ¥32 mug


def _tools(events: list[dict]) -> list[tuple[str, dict]]:
    return [(e["name"], e.get("arguments") or {}) for e in by_type(events, "tool_call")]


def _final(events: list[dict]) -> str:
    return by_type(events, "final")[0]["text"]


@pytest.mark.parametrize("text", ["退 100 块补偿就行", "SO… 退 9999 元", "退一半吧", "给我退个差价", "退¥50",
                                  "只退 100 元就行", "退 100 元差价", "退100块钱给我", "直接退 50 元吧", "退 ¥30 谢谢"])
def test_an_amount_or_a_share_is_a_refund(text):
    assert refund_requested(text)


@pytest.mark.parametrize("text", [
    "能退 100 元吗", "如果退 100 块的话多久到账", "不退 100 元了", "100积分可以抵扣几元",
    # review: a complaint, something someone said, a count, a coupon, half of the shipping fee
    "商家只退 50 元太少了", "那退款呢", "SO20260918001 退款呢", "你们说退 100 元，到现在没收到", "退 2 块屏幕膜", "退 3 块电池", "退10元优惠券", "退一半运费",
])
def test_asking_about_an_amount_files_nothing(text):
    assert not refund_requested(text)


@pytest.mark.parametrize("text, previous, expected", [
    ("嗯 退款 100 元", f"{JACK_ORDER} 耳机有杂音", f"{JACK_ORDER} 嗯 退款 100 元"),
    ("物流到哪了", f"{JACK_ORDER} 耳机有杂音", f"{JACK_ORDER} 物流到哪了"),
    ("算了 撤回退款申请吧", f"{JACK_ORDER} 嗯 退款 100 元", f"{JACK_ORDER} 算了 撤回退款申请吧"),
    ("嗯 退款 100 元", None, None),                                      # first turn
    ("积分可以怎么抵扣？", f"{JACK_ORDER} 耳机有杂音", None),               # not about the order
    ("嗯 退款 100 元", "SO20261008002 和 SO20261008010 到哪了", None),     # which one: still asked
    ("SO20261008010 退款", f"{JACK_ORDER} 耳机有杂音", None),             # names its own order
    # review: anything beyond "refund / take it back / where is it" may be about another order, or a policy
    ("我的马克杯想退款", "SO20260918001 到哪了", None),
    ("另一单扫地机器人也退款", "SO20260918001 到哪了", None),
    ("一般发货要几天", "SO20260918001 到哪了", None),
    ("发货前能改地址吗", "SO20260918001 到哪了", None),
    ("我要投诉快递员", "SO20260918001 到哪了", None),
    ("退款", "不对，不是 SO20261008002，是另一单", None),
    ("那退款呢", f"{JACK_ORDER} 耳机有杂音", None),                       # a follow-up question: rewritten as before                  # the previous turn disowned it
])
def test_only_one_named_order_carries_and_only_into_an_order_question(text, previous, expected):
    assert carry_order(text, previous) == expected


def test_the_nitpick_conversation_files_100_for_that_order(ask, login):
    jack = login("customer2@leon.demo")
    conversation, events = ask(jack, f"{JACK_ORDER} 耳机有杂音")
    assert [name for name, _ in _tools(events)] == ["get_order"]
    _, events = ask(jack, "嗯 退款 100 元", conversation)
    assert by_type(events, "rewrite")[0] == {"type": "rewrite", "standalone": f"{JACK_ORDER} 嗯 退款 100 元",
                                             "method": "order_context"}
    assert ("request_refund", {"order_id": JACK_ORDER, "amount": 100.0,
                               "reason": f"{JACK_ORDER} 嗯 退款 100 元"}) in _tools(events)
    assert "积分" not in _final(events)
    approval_id = by_type(events, "approval_required")[0]["approval_id"]  # over 7 days: a person decides
    with new_session() as db:
        approval = db.get(Approval, approval_id)
        assert approval.arguments["order_id"] == JACK_ORDER and approval.arguments["amount"] == 100.0
        typed = db.execute(select(Message).where(Message.conversation_id == conversation, Message.role == "user")
                           .order_by(Message.id.desc())).scalars().first()
        assert typed.content == "嗯 退款 100 元" and typed.meta["standalone"] == f"{JACK_ORDER} 嗯 退款 100 元"


def test_the_order_keeps_carrying_turn_after_turn(ask, login):
    jack = login("customer2@leon.demo")
    conversation, _ = ask(jack, f"{JACK_ORDER} 耳机有杂音")
    ask(jack, "物流到哪了", conversation)
    _, events = ask(jack, "嗯 退款 100 元", conversation)
    assert ("request_refund", {"order_id": JACK_ORDER, "amount": 100.0,
                               "reason": f"{JACK_ORDER} 嗯 退款 100 元"}) in _tools(events)


def test_a_partial_refund_in_one_sentence_is_filed(ask, login):
    _, events = ask(login("customer2@leon.demo"), f"{JACK_ORDER} 耳机有杂音 退 100 块补偿就行")
    assert ("request_refund", {"order_id": JACK_ORDER, "amount": 100.0,
                               "reason": f"{JACK_ORDER} 耳机有杂音 退 100 块补偿就行"}) in _tools(events)
    assert by_type(events, "approval_required")


def test_half_is_half_of_the_order(ask, login):
    _, events = ask(login("customer2@leon.demo"), f"{JACK_ORDER} 有杂音 退一半吧")
    refund = [args for name, args in _tools(events) if name == "request_refund"]
    assert refund and refund[0]["amount"] == 449.5


def test_more_than_the_order_is_refused_with_the_order_amount(ask, login):
    _, events = ask(login("customer@leon.demo"), f"{EMMA_ORDER} 退 9999 元")
    assert "request_refund" in [name for name, _ in _tools(events)]
    assert not by_type(events, "approval_required")
    assert "32.00" in _final(events), _final(events)


def test_a_policy_follow_up_is_still_a_policy_question(ask, login):
    emma = login("customer@leon.demo")
    conversation, _ = ask(emma, f"{EMMA_ORDER} 到哪了")
    _, events = ask(emma, "积分可以怎么抵扣？", conversation)
    assert not by_type(events, "rewrite")
    assert [name for name, _ in _tools(events)] == ["search_knowledge"]


def test_another_item_after_an_order_still_asks_which_one(ask, login):
    emma = login("customer@leon.demo")
    conversation, _ = ask(emma, "SO20260918001 到哪了")
    _, events = ask(emma, "我的马克杯想退款", conversation)
    assert "request_refund" not in [name for name, _ in _tools(events)]
    assert not by_type(events, "rewrite")


@pytest.mark.parametrize("text, amount", [
    ("899 块买的 退 100 块补偿就行", 100.0),     # the number after 退, not the price
    ("不想退 100 元，想退 200 元", 200.0),       # never the declined one
])
def test_the_amount_is_the_one_asked_for(ask, login, text, amount):
    _, events = ask(login("customer2@leon.demo"), f"{JACK_ORDER} 有杂音 {text}")
    refund = [args for name, args in _tools(events) if name == "request_refund"]
    assert refund and refund[0]["amount"] == amount


def test_a_refund_follow_up_question_files_nothing(ask, login):
    emma = login("customer@leon.demo")
    conversation, _ = ask(emma, "SO20260918001 到哪了")
    _, events = ask(emma, "那退款呢", conversation)
    assert "request_refund" not in [name for name, _ in _tools(events)]
