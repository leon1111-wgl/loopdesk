"""Saying "先别退款" or asking "如果退款的话…" must never move money (N-091). The offline model answers
the question instead, and the tool refuses a refund the customer's own words decline, whatever a
real model makes of them."""

import pytest
from sqlalchemy import select

from app.db import new_session
from app.models import Approval, Order, Refund
from app.services import get_services
from app.textutil import refund_declined, refund_requested
from tests.conftest import by_type
from tests.test_refund_policy import ScriptedRefundModel

DECLINED = [
    ("customer@leon.demo", "SO20260918001", "先别退款啊！我就问问 SO20260918001 如果退款的话钱退到哪"),
    ("customer@leon.demo", "SO20261012003", "我不想退款哈 只是问问 SO20261012003 要是退款的话多久到账"),
    ("customer@leon.demo", "SO20261008010", "不要退款，我要换货 SO20261008010 尺寸不对"),
    ("customer@echo.demo", "SO20261019101", "SO20261019101 如果退款的话 我收藏的歌单还在吗"),
    ("customer@echo.demo", "SO20261014103", "I don't want a refund, just asking: SO20261014103 refund rules?"),
    # N-095: a request word inside an "if" clause asks what would happen.
    ("customer@echo.demo", "SO20261019101", "SO20261019101 如果我申请退款，会员期间下载的歌还能听吗"),
    ("customer@leon.demo", "SO20261008010", "SO20261008010 如果我申请退款，优惠券会退吗"),
]


def _nothing_filed(order_id: str) -> bool:
    with new_session() as db:
        refunds = db.execute(select(Refund).where(Refund.order_id == order_id)).scalars().all()
        approvals = db.execute(select(Approval).where(Approval.tool_name == "request_refund")).scalars().all()
        return not refunds and not [a for a in approvals if a.arguments.get("order_id") == order_id]


@pytest.mark.parametrize("text", [text for _, _, text in DECLINED] + [
    "先不退了 SO20261008002", "SO20261008002 暂时不退款，想问下退款规则", "what if I refund SO20261019101, do I keep my playlists?",
    # Found by review: bare and colloquial declines, and questions with no "如果".
    "不退款，我要换货 SO20260918001 尺寸不对", "先不退款，想问问 SO20260918001 退到哪", "我没说要退款 SO20260918001，就问问多久到账",
    "不退款了 SO20261008002", "SO20261008002 退款就算了", "取消退款申请 SO20261008002", "别退我的钱 SO20261008002",
    "我不是想退款，只是想知道能不能换", "No refund, I want an exchange SO20261019101", "I did not request a refund",
    "I haven't asked for a refund", "What if I want a refund for SO20261019101, how long does it take?",
    "SO20260918001 退款是原路退回吗", "退款会扣手续费吗 SO20260918001", "SO20261008002 能退款吗？",
    # N-095: request words inside an "if" clause, ending at a comma, a space, "的话" or the end of the message.
    "SO20261008010 要是我要退款，运费谁出？", "SO20261008010 If I request a refund, will my coupon come back?",
    "SO20261008010 如果我想退款的话要多久", "SO20261008010 如果申请退款 钱退到哪", "如果我申请退款会员期间的歌还能听吗 SO20261019101",
    "SO20261008010 要是帮我退款了，积分还在吗", "if I want a refund for SO20261008010 is shipping refunded?",
    "如果那个订单我申请退款，会员还能用吗", "SO20261008010 如果我申请退款\n优惠券会退吗",
    # Found by review: "就" or "则" inside the condition, other conditionals, and "…的话 / …后" after a request word.
    "SO20261019101 如果我现在就申请退款，会员期间下载的歌还能听吗", "SO20261019101 如果我今天就退款，优惠券还能退吗",
    "SO20261008010 如果按规则申请退款，优惠券会退吗", "如果 我申请退款，优惠券会退吗", "一旦我申请退款，下载的歌还能听吗",
    "若申请退款，会员还能用吗", "我申请退款后会员期间下载的歌还能听吗", "申请退款之后优惠券会退吗", "想问下申请退款后钱退到哪",
    "我申请退款的话优惠券会退吗", "我要退款的话多久到账", "我想退款的话需要退货吗", "如果我申请退款，钱会退我原来的卡吗",
    "SO20261008010 If I request a refund will I lose my downloads", "if I want a refund for SO20261008010 is shipping refunded",
    "SO20261019101 如果我现在就申请退款，会员应该就没了吧", "我申请退款的时候需要退货吗", "申请退款时运费谁出", "我申请退款会不会影响会员",
    "想问一下申请退款需要退货吗", "咨询一下申请退款要几天", "申请退款了的话优惠券还能用吗",
    # "就退" after "如果我…" is ambiguous ("如果我今天就退款" vs "如果不能换货就退款"); with a question after it, money stays put.
    "SO20261019101 如果我今天就退款，优惠券还能退吗",
])
def test_declining_or_asking_is_not_a_request(text):
    assert not refund_requested(text) and refund_declined(text)


@pytest.mark.parametrize("text", [
    "SO20261008002 我要退款", "订单 SO20260918001 的抱枕不想要了，申请退款", "SO20261019101 刚被扣了续费，帮我退款",
    "SO20261012003 先退我50块吧", "SO20261008002 退货退款", "可以的话帮我退款 SO20261008002",
    "为什么还不给我退款 SO20261008002", "Please refund my order SO20261008002", "不退款我就投诉 SO20261008002",
    "为什么不退款 SO20261008002", "不要换货，我要退款 SO20261008002", "如果不能换货就退款 SO20261008002",
    "如果可以的话请退款 SO20261008002", "按照退款规则退款给我 SO20261008002", "If possible, please refund SO20261008002",
    "好的，那就退款吧", "按政策给客户退款 SO20261008002",
    # The request after the "if" clause still counts (N-095).
    "如果可以的话请直接帮我退款 SO20261008002", "我要退款，如果可以的话今天处理 SO20261008002",
    "杯子碎了，如果你们不换那就给我退款 SO20261008010", "refund me if it has not shipped SO20261008002",
    "如果可以，帮我退款 SO20261008002", "如果不行 帮我退款 SO20261008002", "SO20261008002 如果明天还不发货，我要退款",
    "如果不换货，那我要退款 SO20261008002",
    # A condition without a question is still a request (review: the first N-095 rule turned these down).
    "if it's broken please refund SO20261008002", "If it arrives damaged please refund me", "if you can't replace it I want a refund",
    "if no stock refund me", "如果坏了帮我退款", "如果明天还不发货我要退款", "如果还没发货请帮我退款", "如果没法换货请直接退款吧",
    "要是没货了直接退款", "要是不能发货退我钱", "如果不能换货那退款吧", "帮我退款后通知我一声", "需要是全额退款，帮我退款",
    # A request before the condition, or right after its consequence, still counts when a question follows.
    "我要退款，如果可以的话今天能到账吗", "帮我退款，如果可以的话今天处理，钱退到哪", "refund me if it has not shipped, how long will it take?",
    "I want a refund if it hasn't shipped. Can you do that?", "如果可以的话帮我退款，多久到账？", "要是今天不发货就给我退款，可以吗",
    "if it's broken please refund, where does the money go", "快递一直不动，如果明天还不到怎么办？帮我退款吧", "要是货还在仓库就直接退款吧",
    "如果还能截停就帮我退款", "如果不发货呢，那就帮我退款",
])
def test_requests_still_count(text):
    assert refund_requested(text) and not refund_declined(text)


@pytest.mark.parametrize("text", [
    "如果几天内都不发货就退款", "如果不能换货就退款 谢谢，还有别的问题吗", "如果不能换货就退款，退款多久到账",
    "如果不能换，能不能直接退款", "If it doesn't ship by Friday refund me, do it automatically",
])
def test_unclear_conditional_requests_are_answered_not_filed(text):
    # Known cost of N-095: a bare "就退款" after a condition, with a question anywhere after it, reads like
    # "如果我今天就退款，优惠券还能退吗". These are answered with how to apply; the customer replies "SO… 申请退款".
    assert not refund_requested(text)


def test_a_turn_that_does_not_mention_refunds_is_not_declined():
    # "是的" after the model asked "确认要退吗？" is left to the model.
    assert not refund_declined("是的") and not refund_declined("")


def test_every_gate_case_that_files_a_refund_is_still_a_request():
    import json
    from pathlib import Path
    for path in Path(__file__).parents[1].joinpath("evals").glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            case = json.loads(line)
            if "request_refund" in case.get("expected_tools", []):
                assert refund_requested(case["question"]), case["question"]


@pytest.mark.parametrize(("email", "order_id", "text"), DECLINED)
def test_offline_model_answers_without_filing(ask, login, email, order_id, text):
    _, events = ask(login(email), text)
    assert "request_refund" not in [e["name"] for e in by_type(events, "tool_result")]
    assert not by_type(events, "approval_required")
    assert _nothing_filed(order_id)
    answer = by_type(events, "final")[0]["text"]
    assert "已受理" not in answer and f"“{order_id} 申请退款”" in answer


def test_tool_refuses_a_refund_the_customer_declined(ask, login, monkeypatch):
    # A real model that reads "如果退款的话" as a request: within the automatic limit this used to pay out.
    monkeypatch.setattr(get_services(), "provider",
                        ScriptedRefundModel({"order_id": "SO20260918001", "amount": 39.9, "reason": "顾客咨询"}))
    _, events = ask(login("customer@leon.demo"), DECLINED[0][2])
    result = [e for e in by_type(events, "tool_result") if e["name"] == "request_refund"][0]
    assert "并没有申请退款" in result["output"]["error"]
    assert not by_type(events, "approval_required")
    assert _nothing_filed("SO20260918001")
    with new_session() as db:
        assert db.get(Order, "SO20260918001").status == "delivered"


def test_tool_still_files_an_explicit_request(ask, login, monkeypatch):
    monkeypatch.setattr(get_services(), "provider",
                        ScriptedRefundModel({"order_id": "SO20260918001", "amount": 39.9, "reason": "不想要了"}))
    _, events = ask(login("customer@leon.demo"), "SO20260918001 我要退款")
    result = [e for e in by_type(events, "tool_result") if e["name"] == "request_refund"][0]
    assert "error" not in result["output"] and result["output"]["amount"] == 39.9


# N-126: the question in a complaint ("还不发货？？？") is not a question about the bare demand that follows it.
ANGRY = "😡😡😡 SO20261012003 还不发货？？？ 不要了 退钱！！！"
WHY_NOT_SHIPPED = "SO20261012003 怎么还不发货 退款"
WAITED = "SO20261012003 等了这么久还不发？退款！"
TWO_ORDERS = "SO20261012003和SO20260918011都退了吧"


@pytest.mark.parametrize("text", [
    ANGRY, WHY_NOT_SHIPPED, WAITED, "SO20261012003 退了吧", TWO_ORDERS, "还没发货吗 那退款", "怎么回事啊？？退款",
    "说好的今天发呢？我不要了，退钱", "怎么还不发货 refund", "why no shipping?? refund!!", "怎么还不发货～退款",
    "怎么还不发货\n退款", "ＳＯ２０２６１０１２００３\u3000退了吧", "怎么还不发货啊？麻烦退款", "怎么还不发货？不要了退款",
    "not shipped yet? refund now", "where is it?? refund asap", "why?? full refund", "还不发货？退钱💢", "还不发货？退钱❗❗",
])
def test_a_bare_demand_after_a_complaint_is_a_request(text):
    assert refund_requested(text) and not refund_declined(text)


@pytest.mark.parametrize("text", [
    # A question after the refund word, or about it, is still asked, not filed.
    "退款，多久到账", "SO20261012003 退款 多久到账", "还不发货？退款要几天", "怎么还不发货 退款怎么申请",
    "how do refunds work", "还不发货？？退款能原路退回吗", "退款？", "等了这么久，退款的话运费谁出",
    "还不发货？？？ 不要退钱，我再等等", "怎么还不发货？退款咋弄", "怎么还不发货？退款啥时候到", "还不发货？退款快不快",
    "还不发货？退款要手续费不", "还不发货？退款，到账没",
    # Found by review: a question, then a statement that only mentions a refund. The first version filed these.
    "SO20260918001 怎么还不发货？退款已经申请了", "SO20260918001 咋回事？退款被拒了", "SO20260918001 怎么回事？退款金额不对",
    "SO20260918001 怎么还不发货？退货地址发我一下", "Where is my order SO20260918001? Refund pending for 2 weeks",
    "什么时候到账？退款三天了", "发货了吗？退款我再考虑考虑", "还能发货吗？退款不急", "Hello? refund received, thanks",
    # "退了吧" as a guess that it was refunded, not a demand.
    "SO20260918001 钱应该退了吧？怎么还没到账", "SO20260918001 已经退了吧", "上周就退了吧，钱呢", "你们退了吧？我没收到",
    "退了吧？到账没", "都退了吧，怎么一分钱没收到",
    # Found by the second review: "了 / 啦" after the refund word reports it, it does not ask for it.
    "SO20260918001 怎么回事？都退款了", "SO20260918001 是吗？退款了", "是吗？退款啦",
])
def test_a_question_or_a_statement_about_a_refund_is_not_a_request(text):
    assert not refund_requested(text)


def test_the_demand_check_stays_linear():
    import time
    started = time.perf_counter()
    for text in ("退款，" * 1000 + "吗", "退款\n" * 1000, "退款 " * 1000 + "吗",
                 "那就" * 26 + "退了吧z", "怎么还不发货？" + "退货退款" * 22 + "z", "那就" * 990 + "退了吧z"):
        refund_requested(text)
    assert time.perf_counter() - started < 0.5  # the first version took 2.5 s on the first one; another over 15 s on the fourth


@pytest.mark.parametrize("text", [ANGRY, WHY_NOT_SHIPPED, WAITED])
def test_offline_model_files_the_angry_demand(ask, login, text):
    # ¥268 is over the automatic limit, so the request waits for a person instead of being answered.
    _, events = ask(login("customer@leon.demo"), text)
    approval = by_type(events, "approval_required")
    assert approval and approval[0]["arguments"]["order_id"] == "SO20261012003"
    answer = by_type(events, "final")[0]["text"]
    assert "不会为您发起退款" not in answer and "人工审核" in answer


@pytest.mark.parametrize("text", ["SO20260918001 怎么还不发货？退款已经申请了", "SO20260918001 钱应该退了吧？怎么还没到账"])
def test_offline_model_does_not_pay_out_on_a_statement(ask, login, text):
    # Both paid ¥39.90 automatically in the first version of this fix.
    _, events = ask(login("customer@leon.demo"), text)
    assert "request_refund" not in [e["name"] for e in by_type(events, "tool_result")]
    assert _nothing_filed("SO20260918001")


def test_two_orders_to_refund_asks_which_one(ask, login):
    _, events = ask(login("customer@leon.demo"), TWO_ORDERS)
    assert "request_refund" not in [e["name"] for e in by_type(events, "tool_result")]
    assert "一次只能为一个订单申请退款" in by_type(events, "final")[0]["text"]


def test_an_open_question_asks_instead_of_promising_nothing(ask, login):
    # Only a customer who said "别退" hears "不会为您发起退款"; a plain question is offered the way to apply.
    _, events = ask(login("customer@leon.demo"), "SO20260918001 退款是原路退回吗")
    answer = by_type(events, "final")[0]["text"]
    assert "不会为您发起退款" not in answer and "如需现在申请退款" in answer
    assert _nothing_filed("SO20260918001")
