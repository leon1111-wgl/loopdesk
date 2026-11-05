"""One message, several things broken: "我买的音箱没声了 杯子也碎了" made one ticket, for the speaker, and the cup was
never mentioned again; "音箱没声了 耳机也坏了" asked which one; two order numbers made none. And a model that did make two
told the customer only the last number: the status template rendered one ticket.
"""

import pytest
from sqlalchemy import select

from app.agent.status_templates import case_state, compose, render_status
from app.db import new_session
from app.llm.mock import several_faults
from app.models import Ticket
from app.services import get_services
from tests.conftest import by_type
from tests.scripted_model import ScriptedModel, call
from tests.test_status_templates import tool

JACK, EMMA = "customer2@leon.demo", "customer@leon.demo"


def _tickets(conversation: int) -> list[Ticket]:
    with new_session() as db:
        return list(db.execute(select(Ticket).where(Ticket.conversation_id == conversation).order_by(Ticket.id)).scalars())


@pytest.mark.parametrize("who,text,titles", [
    (JACK, "我买的音箱没声了 耳机也坏了", ["SO20261015009 蓝牙小音箱 换货/维修", "SO20261003005 降噪耳机 Pro 换货/维修"]),
    (JACK, "音箱没声了，还有那个降噪耳机也坏了", ["SO20261015009 蓝牙小音箱 换货/维修", "SO20261003005 降噪耳机 Pro 换货/维修"]),
    (JACK, "我买的音箱没声了 杯子也碎了", ["SO20261015009 蓝牙小音箱 换货/维修", "杯子也碎了"]),
    (EMMA, "我买的抱枕开线了 扫地机器人也坏了", ["SO20260918001 云朵抱枕 换货/维修", "扫地机器人也坏了"]),
    (EMMA, "SO20260918001 的抱枕开线了 SO20261008010 的杯子裂了",
     ["SO20260918001 云朵抱枕 换货/维修", "SO20261008010 陶瓷马克杯 换货/维修"]),
    (EMMA, "SO20260918001 的抱枕开线了 马克杯也裂了", ["SO20260918001 云朵抱枕 换货/维修", "SO20261008010 陶瓷马克杯 换货/维修"]),
    (JACK, "我买的音箱 没声了 耳机 也坏了", ["SO20261015009 蓝牙小音箱 换货/维修", "SO20261003005 降噪耳机 Pro 换货/维修"]),
])
def test_each_thing_reported_gets_its_ticket(ask, login, who, text, titles):
    conversation, events = ask(login(who), text)
    tickets = _tickets(conversation)
    assert [t.title for t in tickets] == titles, text
    final = by_type(events, "final")[0]["text"]
    for ticket in tickets:
        assert f"工单 #{ticket.id}" in final, "the customer is told every number"
    assert by_type(events, "approval_required") == [], "a report moves no money"


def test_a_question_after_the_reports_is_still_answered(ask, login):
    conversation, events = ask(login(JACK), "我买的音箱没声了 杯子也碎了 另外积分怎么抵扣")
    assert [t.title for t in _tickets(conversation)] == ["SO20261015009 蓝牙小音箱 换货/维修", "杯子也碎了"]
    final = by_type(events, "final")[0]["text"]
    assert "另外，" in final and "积分" in final, final


def test_each_ticket_goes_to_its_team_with_its_own_words(ask, login):
    conversation, events = ask(login(JACK), "我买的音箱没声了 杯子也碎了")
    speaker, cup = _tickets(conversation)
    assert speaker.description == "我买的音箱没声了" and cup.description == "杯子也碎了"
    final = by_type(events, "final")[0]["text"]
    assert final.index("技术支持") < final.index("售后专员"), final


def test_more_than_fit_in_a_run_go_on_the_last_ticket(ask, login):
    conversation, events = ask(login(JACK), "我买的音箱没声了 耳机也坏了 风扇也坏了 杯子也碎了")
    tickets = _tickets(conversation)
    assert len(tickets) == 3 and "风扇也坏了" in tickets[2].description and "杯子也碎了" in tickets[2].description
    assert by_type(events, "final")[0]["status"] != "handoff", "within the run's steps"


def test_a_second_thing_wrong_with_the_same_order_goes_on_its_ticket(ask, login):  # second-model review P2
    conversation, events = ask(login(EMMA), "SO20260918001 的抱枕开线了 SO20260918001 的拉链也坏了")
    ticket, = _tickets(conversation)
    assert "抱枕开线了" in ticket.description and "拉链也坏了" in ticket.description
    assert "分开处理" not in by_type(events, "final")[0]["text"], "one ticket: nothing to tell apart"


def test_a_number_that_is_not_theirs_gets_no_ticket(ask, login):
    conversation, events = ask(login(EMMA), "SO20261015009 的音箱坏了 SO20261008010 的杯子裂了")
    assert [t.title for t in _tickets(conversation)] == ["SO20261008010 陶瓷马克杯 换货/维修"]
    assert "未能查询到该订单" in by_type(events, "final")[0]["text"]


@pytest.mark.parametrize("text", [
    "音箱没声了 是不是坏了", "我买的音箱坏了 而且也打不开", "我买的音箱坏了 屏幕也碎了", "我买的音箱坏了 订单有问题", "我买的蓝牙音箱坏了", "买的扫地机器人坏了，按电源键也开不了机，很紧急", "我买的音箱坏了 音箱的屏幕也碎了", "我买的音箱才三天就坏了 用了两天也没声",
    "音箱坏了 是我自己摔的", "音箱是我自己摔的 现在打不开了", "音箱坏了 耳机也坏了 不过是我自己摔的",
    "音箱坏了 杯子没坏", "杯子会不会碎 音箱容易坏吗", "我买的音箱坏了 杯子也碎了 我要退款", "音箱坏了 杯子也碎了 转人工",
    "P1故障多久响应 P2故障呢"])
def test_one_report_or_a_question_is_not_several(text):
    assert several_faults(text) == [], text


@pytest.mark.parametrize("text,kept", [
    ("我买的音箱坏了 耳机也坏了 不过耳机是我自己摔的", ["SO20261015009 蓝牙小音箱 换货/维修"]),
    ("杯子是我自己摔碎的 音箱也坏了", ["SO20261015009 蓝牙小音箱 换货/维修"])])
def test_what_they_damaged_gets_no_ticket_and_the_rest_still_does(ask, login, text, kept):  # second-model review
    conversation, events = ask(login(JACK), text)
    assert [t.title for t in _tickets(conversation)] == kept, text
    final = by_type(events, "final")[0]["text"]
    assert "已为您创建工单" in final and "转人工" in final, final


def test_a_part_word_inside_a_things_name_does_not_hide_it():
    assert several_faults("我买的蓝牙音箱坏了 电源适配器也坏了") == ["我买的蓝牙音箱坏了", "电源适配器也坏了"]


def test_a_model_that_makes_two_tickets_tells_both_numbers(ask, login, monkeypatch):
    monkeypatch.setattr(get_services(), "provider", ScriptedModel(
        call("create_ticket", title="音箱没声了", description="音箱没声了", priority="normal"),
        call("create_ticket", title="杯子碎了", description="杯子碎了", priority="normal"),
        "工单已提交，我们会跟进这两件。"))
    conversation, events = ask(login(JACK), "我买的音箱没声了 杯子也碎了")
    first, second = _tickets(conversation)
    final = by_type(events, "final")[0]["text"]
    assert f"工单 #{first.id}" in final and f"工单 #{second.id}" in final, final


def test_the_template_renders_every_ticket_once():
    one = tool(1, "create_ticket", {"ticket_id": 5, "priority": "normal", "sla_hours": 24, "team": "技术支持"})
    two = tool(2, "create_ticket", {"ticket_id": 6, "priority": "high", "sla_hours": 4, "team": "售后专员"})
    state = case_state([one, two], "")
    assert render_status(state).ticket == ("已为您创建工单 #5（优先级：普通），技术支持将在 24 小时内联系您。"
                                          "已为您创建工单 #6（优先级：高），售后专员将在 4 小时内联系您。")
    assert compose("已为您创建工单。已为您创建工单。谢谢。", state).text.count("工单 #5") == 1
    failed = tool(3, "create_ticket", {"error": "系统繁忙"})
    assert case_state([one, failed], "").ticket == "created", "the one that was made still stands"
