"""What customers say that the offline model and the rewriter did not follow (10-28 ① and older rounds).

Each test is one nitpick item's reproduction; the offline model is rule-based, so these pin the rules, and the
same sentences are cases in evals/leon_default.jsonl. A real model reads them itself; what it shares with the
offline one is tested here too: the order tools, the rewriter, the queue, the ticket wording and the refund guard.
"""

import pytest
from sqlalchemy import func, select

from app.agent.rewrite import carry_order_loosely, rule_rewrite
from app.db import new_session
from app.llm.mock import order_clauses, resolve_ordinal, smalltalk
from app.llm.base import LLMMessage
from app.models import Approval, Ticket
from app.textutil import fix_typos
from tests.conftest import by_type


def _tools(events):
    return [e["name"] for e in by_type(events, "tool_call")]


def _final(events):
    return by_type(events, "final")[0]["text"]


def _approvals() -> int:
    with new_session() as db:
        return db.execute(select(func.count(Approval.id))).scalar_one()


# ---------------------------------------------------------------- parcels
@pytest.mark.parametrize("text", ["SO20261008002 怎么还没到", "SO20261008002 到那了", "SO20261008002 咋还没送到"])
def test_chasing_a_parcel_tracks_it(ask, login, text):  # N-219
    _, events = ask(login("customer@leon.demo"), text)
    assert "track_shipment" in _tools(events) and "AT1402938475" in _final(events)


def test_why_so_slow_after_tracking_stays_on_the_order(ask, login):  # N-220
    emma = login("customer@leon.demo")
    conversation, _ = ask(emma, "SO20261008002 到哪了")
    for text in ("为什么这么慢", "那你说怎么办"):
        _, events = ask(emma, text, conversation_id=conversation)
        assert "track_shipment" in _tools(events) and "如需催件" in _final(events), _final(events)
        assert "知识库中没有找到" not in _final(events)


def test_when_will_it_arrive_quotes_the_delivery_time(ask, login):  # N-060
    emma = login("customer@leon.demo")
    conversation, _ = ask(emma, "SO20261008002 到哪了")
    _, events = ask(emma, "那大概几号能到", conversation_id=conversation)
    assert "1-3天送达" in _final(events)


def test_a_delivered_order_without_tracking_is_not_unshipped(ask, login):  # N-061
    _, events = ask(login("customer2@leon.demo"), "SO20261015009 到哪了")
    assert "签收" in _final(events) and "未发货" not in _final(events)


def test_another_order_is_not_the_one_just_tracked(ask, login):  # N-177
    emma = login("customer@leon.demo")
    conversation, _ = ask(emma, "SO20261008002 到哪了")
    _, events = ask(emma, "算了 另一单呢", conversation_id=conversation)
    assert "track_shipment" not in _tools(events) and "SO20261008002" not in _final(events)
    assert "SO20261012003" in _final(events)
    assert "SO20261008002" not in rule_rewrite("那另一个订单呢", "SO20261008002 到哪了")


def test_shown_as_delivered_but_not_received_opens_a_ticket(ask, login):  # N-157
    _, events = ask(login("customer@leon.demo"), "SO20261008002 显示签收了 但我没收到")
    final = _final(events)
    assert "create_ticket" in _tools(events) and "AT1402938475" in final and "售后专员将在 4 小时内" in final, final


def test_rushing_shipment_quotes_the_shipping_time(ask, login):  # N-157
    _, events = ask(login("customer@leon.demo"), "SO20261012003 催发货 今天能发吗")
    assert "48小时内发货" in _final(events) and "转人工" in _final(events)


def test_an_address_change_is_a_ticket_for_the_after_sales_team(ask, login):  # N-157, N-063
    _, events = ask(login("customer@leon.demo"), "SO20261012003 能不能改个地址 我搬家了")
    assert "create_ticket" in _tools(events) and "售后专员" in _final(events)
    with new_session() as db:
        assert db.execute(select(Ticket.title)).scalars().all() == ["SO20261012003 修改收货信息"]


def test_cancelling_an_unshipped_order_files_its_refund_for_review(ask, login):  # N-063
    _, events = ask(login("customer@leon.demo"), "SO20261012003 还没发货吧 不想要了 帮我取消订单")
    assert by_type(events, "approval_required")
    with new_session() as db:
        approval = db.execute(select(Approval)).scalar_one()
    assert approval.arguments["amount"] == 268.0 and "取消未发货订单" in approval.arguments["reason"]


# ---------------------------------------------------------------- refunds and repairs
def test_two_orders_two_requests_in_one_message(ask, login):  # N-094
    _, events = ask(login("customer@leon.demo"), "SO20261008002 到哪了 另外 SO20261008010 杯子能退吗")
    final = _final(events)
    assert _tools(events).count("track_shipment") == 1 and "AT1402938475" in final
    assert "超过 7 天无理由退货期限" in final and "回复“SO20261008010 申请退款”" in final and _approvals() == 0


def test_a_repair_instead_of_a_refund_is_a_ticket(ask, login):  # N-198
    jack = login("customer2@leon.demo")
    conversation, _ = ask(jack, "SO20261015009 音箱没声音了 退款的话多久到账")
    _, events = ask(jack, "不对 我不要退款了 修一下就行", conversation_id=conversation)
    assert "create_ticket" in _tools(events) and "list_my_orders" not in _tools(events) and _approvals() == 0
    assert "技术支持" in _final(events) or "售后专员" in _final(events)


def test_an_angry_follow_up_keeps_the_order_and_is_offered_the_refund(ask, login):  # N-199
    emma = login("customer@leon.demo")
    conversation, _ = ask(emma, "SO20261008010 杯子收到就是碎的")
    _, events = ask(emma, "你没听懂吗 我说碎了！！要退钱", conversation_id=conversation)
    assert "“SO20261008010 申请退款”" in _final(events) and _approvals() == 0
    _, events = ask(emma, "SO20261008010 申请退款", conversation_id=conversation)  # named: filed (quality issue)
    assert by_type(events, "approval_required")


def test_damage_the_customer_caused_is_not_a_quality_problem(ask, login):  # N-200
    _, events = ask(login("customer@leon.demo"), "我昨天买的那个杯子摔坏了是我自己摔的能退吗")
    assert "create_ticket" not in _tools(events) and "人为损坏" in _final(events) and "技术支持" not in _final(events)


def test_a_mug_ticket_is_not_for_technical_support(ask, login):  # N-200
    _, events = ask(login("customer@leon.demo"), "SO20261008010 杯子开线了 帮我换个新的")
    assert "售后专员" in _final(events)
    _, events = ask(login("customer2@leon.demo"), "SO20261003005 耳机连不上 帮我报修")
    assert "技术支持" in _final(events)


def test_a_threat_to_complain_later_is_answered_now(ask, login):  # N-129
    _, events = ask(login("customer@leon.demo"), "SO20261008002 到哪了啊 都三天了 再不到我就差评投诉！！")
    assert "handoff_to_human" not in _tools(events) and "AT1402938475" in _final(events)
    assert "如需投诉，可以回复“转人工”" in _final(events)
    _, events = ask(login("customer@leon.demo"), "我要投诉")  # a complaint now still goes to a person
    assert by_type(events, "final")[0]["status"] == "handoff"


def test_leaving_the_queue_hands_back_to_the_agent(client, ask, login):  # N-062
    jack = login("customer2@leon.demo")
    conversation, events = ask(jack, "转人工")
    assert by_type(events, "final")[0]["status"] == "handoff"
    _, events = ask(jack, "算了 不用人工了 退款多久到账", conversation_id=conversation)
    assert "1-3个工作日" in _final(events)
    with new_session() as db:
        ticket = db.execute(select(Ticket).where(Ticket.kind == "handoff")).scalar_one()
    assert ticket.status == "closed" and "顾客取消" in ticket.resolution
    conversation, _ = ask(jack, "转人工")
    _, events = ask(jack, "取消排队", conversation_id=conversation)
    assert _final(events) == "好的，已为您取消人工排队。还有其他问题可以继续问我。"


def test_a_person_who_already_took_the_ticket_keeps_the_conversation(client, ask, login):  # N-062
    jack = login("customer2@leon.demo")
    conversation, _ = ask(jack, "转人工")
    with new_session() as db:
        ticket_id = db.execute(select(Ticket.id).where(Ticket.kind == "handoff")).scalar_one()
    client.post(f"/api/handoffs/{ticket_id}/reply", json={"content": "您好，我是 Mia"}, headers=login("agent@leon.demo"))
    _, events = ask(jack, "算了 不用人工了", conversation_id=conversation)
    assert by_type(events, "final")[0]["status"] == "handoff"


# ---------------------------------------------------------------- listings, greetings, typos
def test_my_orders_then_the_second_one(ask, login):  # N-057, N-093
    emma = login("customer@leon.demo")
    conversation, events = ask(emma, "我的订单有哪些")
    assert _tools(events) == ["list_my_orders"]
    _, events = ask(emma, "第二单", conversation_id=conversation)
    assert "SO20261008002" in _final(events) and "知识库中没有找到" not in _final(events)


def test_the_first_one_after_a_refund_listing_is_offered_not_filed(ask, login):  # N-093
    emma = login("customer@leon.demo")
    conversation, _ = ask(emma, "我要退款")
    _, events = ask(emma, "第一个", conversation_id=conversation)
    assert "回复“SO20261012003 申请退款”" in _final(events) and _approvals() == 0


def test_ordinals_read_the_listing_they_follow():
    listing = LLMMessage(role="assistant", content="您账号下最近的订单有：SO20261008010（杯，已签收）；"
                                                   "SO20260918011（包，已签收）。请问要查询的是哪一单？")
    history = [LLMMessage(role="user", content="我的订单"), listing]
    assert resolve_ordinal("第二个", history) == "SO20260918011 查一下"
    assert resolve_ordinal("最后那个", history) == "SO20260918011 查一下"
    assert resolve_ordinal("第三个", history) is None and resolve_ordinal("第二个", history[:1]) is None


@pytest.mark.parametrize("text, kind", [("你好", "greeting"), ("在吗？", "greeting"), ("？？？", "greeting"),
                                        ("谢谢", "thanks"), ("好的谢谢", "thanks"), ("你好，我要退款", None)])
def test_smalltalk_is_answered_without_a_search(text, kind):  # N-076
    reply = smalltalk(text)
    assert (reply is None) == (kind is None)


def test_a_greeting_is_not_a_refusal(ask, login):  # N-076
    _, events = ask(login("customer@leon.demo"), "你好")
    assert _tools(events) == [] and "请问有什么可以帮您" in _final(events)


@pytest.mark.parametrize("typed, meant", [("运废险怎么陪", "运费险怎么赔"), ("发漂怎么开", "发票怎么开"),
                                          ("积分会不会过其", "积分会不会过期"), ("积分会不会过其？", "积分会不会过期？"),
                                          ("SO1 推款", "SO1 退款")])
def test_common_typos(typed, meant):  # N-190
    assert fix_typos(typed) == meant


def test_a_typo_still_finds_the_rule(ask, login):  # N-190
    _, events = ask(login("customer@leon.demo"), "运废险怎么陪")
    assert "运费险" in _final(events) and "知识库中没有找到" not in _final(events)


# ---------------------------------------------------------------- the rewriter
@pytest.mark.parametrize("text, carried", [
    ("为什么这么慢", True), ("你没听懂吗 我说碎了！！要退钱", True), ("不对 我不要退款了 修一下就行", True),
    ("那另一单呢", False), ("运费险怎么赔", False), ("我的马克杯一般多久发货", False), ("SO20261008002 到哪了", False),
])
def test_what_carries_the_order_loosely(text, carried):
    assert (carry_order_loosely(text, "SO20261008010 杯子收到就是碎的") is not None) == carried


def test_each_order_keeps_its_own_words():
    assert order_clauses("SO20261008002 到哪了 另外 SO20261008010 杯子能退吗") == {
        "SO20261008002": "SO20261008002 到哪了 另外 ", "SO20261008010": "SO20261008010 杯子能退吗"}


@pytest.mark.parametrize("text, robot, pillow", [
    # the simulated customer's wording (simulations dev/leon_two_things, run 42664792823): each number closes its own sentence
    ("我的扫地机器人物流到哪了？订单号SO20261008002。另外想退三天前收到的抱枕，订单号SO20260918001。", "物流", "想退"),
    ("扫地机器人到哪了 单号是SO20261008002，抱枕想退，订单号SO20260918001", "到哪", "想退"),
    ("SO20261008002 物流到哪了，另外想退抱枕，订单号SO20260918001", "物流", "想退"),
    # the review's wordings: a few closing words after the number, or an ASCII full stop
    ("SO20261008002 到哪了，抱枕想退 SO20260918001 可以吗", "到哪", "想退"),
    ("SO20261008002 到哪了，抱枕想退，订单号SO20260918001 谢谢", "到哪", "想退"),
    ("SO20261008002 到哪了，抱枕想退，订单号是 SO20260918001 对吧", "到哪", "想退"),
    ("SO20261008002 到哪了，抱枕想退 SO20260918001 这单", "到哪", "想退"),
    ("SO20261008002到哪了,抱枕想退,订单号SO20260918001.", "到哪", "想退"),
])
def test_a_number_at_the_end_of_its_sentence_owns_that_sentence(text, robot, pillow):
    clauses = order_clauses(text)
    assert robot in clauses["SO20261008002"] and "退" not in clauses["SO20261008002"]
    assert pillow in clauses["SO20260918001"]


@pytest.mark.parametrize("text", [
    "SO20261008002 到哪了，抱枕想退，SO20260918001 帮忙看看",  # the refund words sit between the two numbers
    "SO20260918001 到哪了，杯子想退，SO20261008010 帮忙看下",  # the other way round: a wrong guess would offer the ¥39.90 pillow
])
def test_refund_words_between_two_numbers_get_a_question_not_a_guess(ask, login, text):
    from app.llm.mock import ASK_WHICH_REFUND_TEXT

    _, events = ask(login("customer@leon.demo"), text)
    answer = by_type(events, "final")[0]["text"]
    assert "申请退款”即可" not in answer and ASK_WHICH_REFUND_TEXT in answer
    assert not by_type(events, "approval_required")


def test_two_asks_with_trailing_numbers_get_the_right_answers(ask, login):
    """Simulations found the agent offering the robot's refund (¥1299) for the pillow's request, and no tracking number."""
    _, events = ask(login("customer@leon.demo"),
                    "我的扫地机器人物流到哪了？订单号SO20261008002。另外想退三天前收到的抱枕，订单号SO20260918001。")
    text = by_type(events, "final")[0]["text"]
    assert "SO20261008002 申请退款" not in text
    assert "SO20260918001 申请退款" in text
    assert "AT1402938475" in text


# ---------------------------------------------------------------- second-model review
@pytest.mark.parametrize("text", ["不要人工智能回复 我要真人", "别取消排队", "退款不需要人工审核吗"])
def test_asking_for_a_person_in_the_queue_keeps_the_place(ask, login, text):
    jack = login("customer2@leon.demo")
    conversation, _ = ask(jack, "转人工")
    _, events = ask(jack, text, conversation_id=conversation)
    assert by_type(events, "final")[0]["status"] == "handoff"
    with new_session() as db:
        assert db.execute(select(Ticket.status).where(Ticket.kind == "handoff")).scalar_one() == "open"


def test_a_warranty_question_is_not_a_repair_ticket(ask, login):
    _, events = ask(login("customer@leon.demo"), "SO20261008002 保修期多久")
    assert "create_ticket" not in _tools(events) and "search_knowledge" in _tools(events)


@pytest.mark.parametrize("text", ["我不要人工了", "我不排了", "不用人工了谢谢"])
def test_leaving_the_queue_in_other_words(ask, login, text):
    jack = login("customer2@leon.demo")
    conversation, _ = ask(jack, "转人工")
    _, events = ask(jack, text, conversation_id=conversation)
    assert by_type(events, "final")[0]["status"] != "handoff"


def test_what_to_do_about_an_invoice_is_not_chasing_the_parcel(ask, login):
    _, events = ask(login("customer@leon.demo"), "SO20261008002 发票开错了怎么办")
    assert "track_shipment" not in _tools(events) and "催件" not in _final(events)


@pytest.mark.parametrize("text", ["不过其实我想问一下运费险怎么赔", "超过其他订单的运费", "怎么陪你聊天", "明天就快第二天了"])
def test_a_typo_rule_never_rewrites_another_word(text):
    assert fix_typos(text) == text


def test_a_conditional_complaint_with_i_want_still_goes_to_a_person(ask, login):
    _, events = ask(login("customer@leon.demo"), "SO20261008002 要是今天不到我要投诉")
    assert by_type(events, "final")[0]["status"] == "handoff"


# ---------------------------------------------------------------- 11-04 batch
def test_stop_the_renewal_and_refund_this_month_does_both(ask, login):  # N-179
    _, events = ask(login("customer@echo.demo"), "SO20261019101 自动续费怎么关 我不想续了 这个月的也退给我")
    assert "request_refund" in _tools(events)
    text = _final(events)
    assert "已受理" in text and "¥18.00" in text and "管理自动续费" in text
    assert "当前会员期仍然有效" not in text, "the refund ends this period; the renewal answer must not say otherwise"


def test_the_renewal_question_with_a_refund_the_policy_refuses(ask, login):  # N-179
    _, events = ask(login("customer@echo.demo"), "SO20261001104 自动续费怎么关 这个月的也退给我")
    text = _final(events)
    assert "request_refund" not in _tools(events) and "超过 7 天" in text and "管理自动续费" in text


@pytest.mark.parametrize("text,refund", [
    ("SO20261019101 自动续费怎么关 我不想续了 这个月的也退给我", True), ("自动续费怎么关？这个月的钱退给我", True),
    ("自动续费怎么关 这个月的退给我吗", False), ("自动续费怎么关 退款多久到账", False), ("会员费怎么退 退给我", False),
    ("退一半 什么时候到账", False), ("这个月的也退给我 怎么操作", False),
    # second-model review (P0): a condition, "会退", or a demand taken back is not a request
    ("SO20261019101 如果我现在关了自动续费 这个月的会退给我", False), ("关了自动续费的话 会退钱", False),
    ("要是取消订阅 剩下的会退钱", False), ("自动续费怎么关 关了之后钱会退给我", False),
    ("SO20261019101 自动续费怎么关 退给我 算了不退了", False), ("自动续费怎么关 这个月的也退给我 不对 我不要退款了", False),
    ("会员怎么取消 退钱到账没", False), ("SO20261008010 我要退款 算了不退了", False), ("帮我退款 不对 我不要退款了", False),
    ("不是要退货，是要退款", True), ("我不要退货 帮我退款", True),
    # re-review P1: refund only, no return, is still a refund; "不退了我就投诉" threatens
    ("我要退款 不要退货", True), ("帮我退款 不需要退货", True), ("给我退钱 不退货", True), ("我要退款不退货", True),
    ("退我50块 不退货", True), ("我要退款 不退了我就投诉", True)])
def test_a_question_about_another_subject_does_not_make_the_refund_a_question(text, refund):  # N-179
    from app.textutil import refund_requested

    assert refund_requested(text) is refund


@pytest.mark.parametrize("text", ["嗯那个就是我上周买的那个那个音箱 没声了 咋整", "我前几天买的音箱没声了", "SO20261015009 音箱没声了 咋整"])
def test_a_fault_without_the_order_number_finds_the_order(ask, login, text):  # N-223
    _, events = ask(login("customer2@leon.demo"), text)
    assert "create_ticket" in _tools(events) and "request_refund" not in _tools(events)
    answer = _final(events)
    assert "SO20261015009" in answer and "15天内发现质量问题" in answer and "已为您创建工单" in answer


def test_a_fault_ticket_names_a_delivered_order_and_keeps_the_urgency(client, ask, login):  # N-223
    _, events = ask(login("customer@leon.demo"), "我刚买的托特包有划痕 严重")
    assert "get_order" in _tools(events) and "优先级：高" in _final(events)
    titles = [t["title"] for t in client.get("/api/tickets", headers=login("agent@leon.demo")).json()]
    assert any(t.startswith("SO20260918011 帆布托特包") for t in titles), titles


@pytest.mark.parametrize("who,text", [
    ("customer@leon.demo", "我买的扫地机器人坏了开不了机，马上给我处理！"),  # on its way, not delivered
    ("customer@leon.demo", "我买的咖啡套装碎了"),  # not shipped yet
    ("customer2@leon.demo", "我买的收纳盒碎了"),  # refunded already
    ("customer2@leon.demo", "我前几天买的风扇坏了"),  # refunded already
    ("customer2@leon.demo", "我买的扫地机器人坏了开不了机，马上给我处理！"),  # not one of Jack's orders
])
def test_a_fault_with_no_delivered_order_is_a_ticket_without_one(ask, login, who, text):  # N-223, review
    _, events = ask(login(who), text)
    assert "get_order" not in _tools(events) and "create_ticket" in _tools(events)
    assert "已为您创建工单" in _final(events) and "最近的订单有" not in _final(events)


def test_a_conditional_or_retracted_renewal_refund_moves_no_money(ask, login):  # N-179, second-model review P0
    lily = login("customer@echo.demo")
    for text in ("SO20261019101 如果我现在关了自动续费 这个月的会退给我", "SO20261019101 自动续费怎么关 退给我 算了不退了"):
        _, events = ask(lily, text)
        assert "request_refund" not in _tools(events) and "已受理" not in _final(events), text
        assert "SO20261019101 申请退款" in _final(events), "offered, not filed"


def test_refund_only_without_returning_the_item_is_filed(ask, login):  # re-review P1
    _, events = ask(login("customer@leon.demo"), "SO20261008002 我要退款 不退货")
    assert "request_refund" in _tools(events) and by_type(events, "approval_required")
