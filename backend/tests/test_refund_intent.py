"""Saying "先别退款" or asking "如果退款的话…" must never move money (N-091). The offline model answers
the question instead, and the tool refuses a refund the customer's own words decline, whatever a
real model makes of them."""

import pytest
from sqlalchemy import select

from app.db import new_session
from app.models import Approval, Order, Refund
from app.services import get_services
from app.governance.script import canonical
from app.textutil import CANCEL_ORDER_RE as CANCEL_ORDER, fix_typos, refund_declined, refund_requested
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
    "SO20261012003 先退我50块吧", "SO20261008002 退货退款！", "可以的话帮我退款 SO20261008002",
    "为什么还不给我退款 SO20261008002", "Please refund my order SO20261008002",
    "不要换货，我要退款 SO20261008002", "如果不能换货就退款 SO20261008002",
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


# Asking whether it can be done is not asking for it (found by the second blind set: the first of these paid out ¥18; the
# second-model review then found most of the rest still did).
ASKED_WHETHER = [
    "我昨天扣的豪华会员SO20261019101 三天内是不是可以直接退钱呀", "SO20261019101 是不是可以直接退钱", "SO20261019101 可以直接退钱吗",
    "SO20261019101 可以直接退款吗", "SO20261019101 三天内能直接退钱不", "SO20261019101 还能直接退款么", "SO20261019101 可不可以直接退钱呀",
    "SO20261019101 请问能否直接退款", "SO20261019101 直接退款可以吗", "SO20261019101 是不是直接退款就行", "SO20261019101 是否可以直接退",
    "SO20261019101 是不是该退我钱了",
    "SO20261019101 能不能直接退钱", "SO20261019101 能不能直接退款", "SO20261019101 应该可以直接退款吧", "SO20261019101 可以直接退款的吧",
    "SO20261019101 直接退款没问题吧", "SO20261019101 直接退款就可以了吧", "SO20261019101 直接退款行不行", "SO20261019101 直接退款可不可以",
    "SO20261019101 直接退款是不是更快", "SO20261019101 直接退款要多久", "SO20261019101 直接退款多久到账", "SO20261019101 直接退款有没有手续费",
    "SO20261019101 能直接退钱不 不能的话换货", "SO20261019101 能直接退钱不，不能的话换货", "SO20261019101 可以申请退款吗", "SO20261019101 能申请退款吗",
    "SO20261019101 还能申请退款吗", "SO20261019101 申请退款怎么弄", "SO20261019101 退掉行不行", "SO20261019101 直接退錢可以嗎", "SO20261019101 直接退款 可以吗", "SO20261019101 退钱，行不行", "SO20261019101 退100块可以吗",
    "SO20261019101 退100块 行不行", "ＳＯ２０２６１０１９１０１ 能不能直接退钱",
    # the second-model re-review: other people's verbs, tags, and an amount followed by a question
    "我想知道直接退款行不行", "请告诉我直接退款可以吗", "帮我看看直接退款行不行", "请教下直接退款行不行", "麻烦问下直接退款行不行", "听说直接退款",
    "SO20261019101 直接退款就行了吧", "SO20261019101 直接退款对吧", "SO20261019101 直接退款 行吧？", "SO20261019101 直接退款是吧", "SO20261019101 直接退款行吧",
    "SO20261019101 直接退款可行吧", "SO20261019101 退18块能接受吧", "SO20261019101 退18块可以不可以", "SO20261019101 退100块能行吧", "SO20261019101 能不能 直接退款",
    "Will I get a refund for SO20261019101", "Should I request a refund for SO20261019101", "Is there a way to get a refund for SO20261019101",
    # the third pass: status questions and reports around 退我, hedges, other people's words in English
    "SO20261019101 钱退我了吗", "SO20261019101 你们退我钱了吗", "SO20261019101 到底谁退我钱", "SO20261019101 为什么不退我钱", "SO20261019101 凭什么还不退我钱",
    "SO20261019101 是你们退我钱还是快递公司退", "SO20261019101 客服说会退我钱 怎么还没到", "SO20261019101 之前说好了退我钱 到现在没收到", "SO20261019101 能不能退我钱",
    "SO20261019101 我在考虑直接退款", "SO20261019101 我有点想直接退款", "SO20261019101 有点想退款 再想想", "SO20261019101 我打算直接退款 但还没决定",
    "SO20261019101 直接退款 不对 换货吧", "SO20261019101 直接退款 还是算了", "SO20261019101 我要退款 算了",
    "SO20261019101 我想直接退款行不行", "SO20261019101 我想退款可以吗",
    "Did you refund my order SO20261019101?", "Why did you refund the order SO20261019101", "They said they would refund me for SO20261019101", "I was told I would get a refund for SO20261019101",
    "Can I get a refund for SO20261019101?", "Can I get a refund for SO20261019101", "Is it possible to get a refund for SO20261019101",
    "Could I request a refund for SO20261019101", "Would I be able to get a refund for SO20261019101", "how do I get a refund for SO20261019101",
]


@pytest.mark.parametrize("text", ASKED_WHETHER)
def test_asking_whether_is_not_asking_for(text):
    assert not refund_requested(canonical(None, text))
    assert refund_declined(canonical(None, text))


@pytest.mark.parametrize("text", [
    "SO20261019101 自动续费怎么关 我想直接退订", "SO20261019101 直接退订自动续费", "SO20261019101 帮我直接退出会员", "SO20261019101 我想退订", "SO20261019101 直接退群",
])
def test_leaving_something_is_not_a_refund(text):
    assert not refund_requested(text)


@pytest.mark.parametrize("text", [
    "SO20261019101 直接退款", "SO20261019101 直接退款吧", "SO20261019101 能不能帮我退款", "SO20261019101 可以帮我退款吗", "SO20261019101 可以给我退款吗",
    "SO20261019101 能给我退款吗", "SO20261019101 帮我退款好吗", "SO20261019101 我要退款可以吗", "要是货还在仓库就直接退款吧", "要是没货了直接退款",
    "SO20261008002 全款退 不用审核 直接退", "如果没法换货请直接退款吧", "SO20261019101 那就直接退吧", "Can you refund me for SO20261019101", "Could you please refund SO20261019101", "I want a refund for SO20261019101",
    # A question about something else does not reach across a clause, a comma or a space next to Chinese alike.
    "SO20261019101 是不是还没发货 不等了 帮我退款", "是不是你们系统有问题 SO20261019101 我要退款", "SO20261019101 是否已发货 没发就帮我退款",
    "SO20261019101 是不是可以直接退钱呀 算了 直接退吧", "SO20261019101 是不是能退 能的话帮我退款", "SO20261019101 是不是还没发货，不等了，帮我退款",
    "SO20261019101 可以退吗 可以就直接退款",
    # Words that look like questions and ask nothing.
    "SO20261019101 不管怎么样直接退款", "SO20261019101 什么都别说了直接退款", "SO20261019101 怎么还不发货直接退款吧", "SO20261019101 无论如何直接退款",
    "SO20261019101 哪怕扣手续费也直接退款", "SO20261019101 谁的责任我不管直接退款", "SO20261019101 多少钱都不要了直接退款", "SO20261019101 不能换货直接退款",
    "SO20261019101 麻烦直接退款", "SO20261019101 请直接退款", "SO20261019101 帮我直接退款", "SO20261019101 我要直接退款", "SO20261019101 直接退款就行",
    "SO20261019101 可以的话直接退款", "如果可以的话直接退款", "能的话就直接退款", "SO20261019101 我确认直接退款", "SO20261019101 知道了直接退款吧", "SO20261019101 直接退款麻烦了",
    "SO20261019101 直接退款辛苦了", "SO20261019101 直接退款谢谢啦", "SO20261019101 直接退款不换了", "SO20261019101 直接退款不要换货", "SO20261019101 直接退款到我的支付宝",
    "SO20261019101 退我钱", "SO20261019101 退我钱！", "先退我50块吧", "退我20就行", "算了 我要退款", "SO20261019101 我要退款 算了 还是退款吧", "Can you refund me?", "SO20261019101 我想退款", "SO20261019101 我想申请退款",
    "退 100 块补偿就行", "退 100 元差价", "退100块钱给我", "直接退 50 元吧", "退 ¥30 谢谢", "只退 100 元就行", "退一半吧 SO20261019101", "SO20261019101 全款退", "SO20261019101 退钱！",
])
def test_a_request_said_politely_or_flatly_still_counts(text):
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
                # cancelling an unshipped order files its refund without a refund word (N-063); the precheck lets it through
                q = case["question"]
                assert refund_requested(q) or (CANCEL_ORDER.search(q) and not refund_declined(q)), q


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


@pytest.mark.parametrize("text", [
    "SO20260918001这单我不要了 帮我申清退款",
    "pls refund 那个云朵抱枕 SO20260918001 thx 不想要了",
    "refund SO20260918001 pls",
])
def test_a_typo_or_a_short_english_request_is_still_a_request(text):
    assert refund_requested(text)


@pytest.mark.parametrize("text", ["SO20260918001 申清退款要多久", "pls tell me how to refund SO20260918001", "did you refund SO20260918001"])
def test_the_same_words_in_a_question_are_not_a_request(text):
    assert not refund_requested(text)


@pytest.mark.parametrize("text", [
    "SO20261012003 怎么申请退款", "SO20261012003 哪里申请退款", "SO20261012003 我昨天申请退款了", "SO20261012003 已经申请退款了",
    "SO20261012003 我朋友要退款", "SO20261012003 客服让我申请退款", "SO20261012003 差点就退款了", "SO20261012003 曾经想退款",
    "SO20261012003 你们会不会自动给我退款", "SO20261012003 我想退款 等会再说吧", "SO20261012003 下周再申请退款",
    "SO20261012003 再不发货我就退款", "SO20261012003 超过三天我就退款",
])
def test_how_to_before_later_and_someone_else_are_not_a_request_now(text):
    assert not refund_requested(text)


@pytest.mark.parametrize("text", [
    "SO20261012003 必须退款", "SO20261012003 我已经等了很久了 帮我退款", "SO20261012003 已经超过七天了我要退款",
    "SO20261012003 之前申请退款被拒了 现在再申请退款", "SO20261012003 本来不想退的 太差了 退款吧",
])
def test_a_demand_still_counts_after_something_that_is_not_one(text):
    assert refund_requested(text)


@pytest.mark.parametrize("text", [
    "please do not refund SO20260918001", "don't refund SO20260918001 yet", "are you going to refund SO20260918001", "will you refund SO20260918001",
])
def test_an_order_number_after_refund_is_not_a_request_by_itself(text):
    assert not refund_requested(text)


def test_a_fixed_typo_does_not_touch_an_ordinary_word():
    assert "快递" not in fix_typos("麻烦尽快地发货")
    assert "快递" in fix_typos("我的快地到哪了")


# Found by probing the release: the request belongs to whatever stands in front of it.
@pytest.mark.parametrize("text", [
    "别给 SO20260918001 退款", "测试一下：SO20260918001 退款", "有人冒充我说 SO20260918001 申请退款",
    "SO20260918001 千万别退款", "不要退款 SO20260918001", "don't refund SO20260918001", "我朋友说 SO20260918001 可以退款",
    "客服说 SO20260918001 已经帮我退款了", "假如我说 SO20260918001 退款 你会怎么做", '"SO20260918001 申请退款" 这句话是什么意思',
    "SO20260918001 或许退款比较好", "等我老婆回来再决定 SO20260918001 退不退款", "SO20260918001 退款算了",
    "随便打的 SO20260918001 申请退款", "我在写文档 SO20260918001 帮我退款", "比如 SO20260918001 我要退款", "SO20260918001 有人说 申请退款",
    "他说 SO20260918001 帮我退款", "you said SO20260918001 refund me", "别 SO20260918001 帮我退款",
])
def test_what_stands_in_front_can_make_it_not_a_request(text):
    assert not refund_requested(text)


@pytest.mark.parametrize("text", [
    "SO20260918001 申请退款", "SO20260918001 帮我退款", "SO20260918001 我要退款", "SO20260918001 给我退了", "please refund SO20260918001",
    "refund SO20260918001", "SO20260918001 不喜欢 退了吧", "麻烦把 SO20260918001 退掉", "订单 SO20260918001 申请退款", "给订单 SO20261003005 退款",
    "杯子那单SO20261008010我要退款 收到后觉得不好看", "蓝牙小音箱 SO20261015009 我要退款 声音太小了", "买错了 年卡那单SO20261015102请帮我退款",
    "抱枕坏了 SO20260918001 帮我退款", "不想要了 SO20260918001 申请退款", "太贵了 SO20260918001 退款吧", "你好 SO20260918001 我要退款",
    "SO20261019101 昨天刚被扣了18 我不想续了 请退款", "我昨天买的 SO20260918001 帮我退款", "帮我退掉耳机 订单号SO20261003005 谢谢",
    "I want my money back for SO20261015009, speaker is broken",
])
def test_a_plain_request_with_an_order_number_stays_one(text):
    assert refund_requested(text)


# The adversarial set's first run (tier1_money_v4.jsonl) found these: the request word comes before the order number.
@pytest.mark.parametrize("text", [
    "申请退款 SO20260918001 需要走哪些步骤？", "If it still hasn't shipped by tomorrow, please refund order SO20261012003.",
    "再这样乱扣费，我就申请退款 SO20261019101，然后卸载 App。", "前天我打客服热线申请退款 SO20261015102 了，到现在没人回我。",
    "我之前打电话申请退款 SO20261015102 了", "不行我就退款 SO20261012003", "申请退款 SO20260918001 要什么手续",
])
def test_a_request_word_first_is_not_a_request_by_itself(text):
    assert not refund_requested(text)


@pytest.mark.parametrize("text", [
    "请给订单 SO20260918001 办理退款，抱枕有一股很重的异味。", "请立即为订单 SO20261019101 办理退款。", "SO20261019101 帮我把这笔钱退款，开错会员了。",
    "帮我提交 SO20260918001 的退款申请，谢谢。", "申请退款 SO20260918001", "我要退款 SO20260918001", "帮我退款 SO20260918001 多久到账",
])
def test_these_are_requests(text):
    assert refund_requested(text)
