"""Money and approval state after the 10-28/10-29 rounds.

- N-232: "SO… 退款到账时间" filed a refund (paid ¥39.90 under the automatic limit, or sent ¥268 to a reviewer).
  Only a request files one now; the bare "SO… 退款" is answered and offered an application.
- N-128: "我可能会退款 看情况" filed one; "我的钱呢 退款呢！" chases a refund, it does not ask for one.
- N-180: filing again while the first application waits got "退款申请未能提交 … 请勿重复提交".
- N-201 (rest): a refund that paid was overwritten by a later call for the same order that failed on "退款中";
  two refunds of one order in one step with different amounts both ran.
- N-230: an application whose tenant tool was switched off was "approved" and never carried out.
"""

import json

import pytest
from sqlalchemy import func, select

from app.agent.status_templates import case_state, render_status
from app.agent.tools import Evidence
from app.db import new_session
from app.llm.base import ToolCall
from app.models import Approval, Refund, Span
from app.services import get_services
from app.textutil import refund_requested
from tests.conftest import by_type
from tests.scripted_model import ScriptedModel
from tests.test_tenant_tools import VOUCHER, _create, backoffice  # noqa: F401  (fixture)

INQUIRIES = [
    "SO20260918001 退款到账时间", "SO20260918001 退款什么时候到账", "SO20260918001 退款啥时候到",
    "SO20260918001 退款几天到", "SO20260918001 退款时效", "SO20260918001 refund eta",
    "SO20260918001 退款一般几个工作日", "SO20261008010 退款", "SO20261008002 退货退款",
    # N-128: a hedge, and chasing a refund that was never asked for
    "SO20261008010 我可能会退款 看情况", "SO20261008010 也许要退 还在考虑", "SO20261015009 我的钱呢 退款呢！",
    # second-model review: a timing question with a request word in it, what already happened, a complaint
    "SO20260918001 when do I get my money back", "SO20260918001 when do I get my refund", "SO20260918001 什么时候给我退款",
    "SO20261008010 退钱了", "SO20261008010 客服说已经退钱了 我没收到", "SO20261008010 之前退全款了",
    "我已经申请退款了，多久到账", "SO20260918001 为什么不退款", "不退款我就投诉 SO20261008002",
]
REQUESTS = [
    "SO20261008010 退款！", "怎么还不发货 退款", "还不发货？？？ 不要了 退钱！！！", "SO20261008002 我要退款",
    "SO20261012003 先退我50块吧 剩下的再说", "SO20261012003 退款 268 元", "顾客要求全额退款 899 元",
    # N-178, N-156
    "SO20261008010 退三十块就行", "SO20261008002 全款退 不用审核 直接退", "SO20261008010 给我退了",
    "SO20261008010 把钱退回来", "SO20261008002 money back please", "I want my money back for SO20261015009, speaker is broken",
    "上次买的帮我退款", "我要退款，多久到账",
    # re-review: a report or a timing question elsewhere in the message does not hide the request
    "SO20261008002 东西已经坏了帮我退款", "已经收到了帮我退款", "我说了要退款！", "我说过要退款", "等了几天了 帮我退款",
    "几天了还没发货 帮我退款", "多久到账 我要退款", "退款多久到账 帮我退款吧", "什么时候发货 不发就帮我退款",
    "什么时候发货不发就帮我退款", "等了几天都没发货，给我退款",
]


def _refunds(order_id: str) -> int:
    with new_session() as db:
        return db.execute(select(func.count(Refund.id)).where(Refund.order_id == order_id)).scalar_one()


def _applications(order_id: str) -> list[Approval]:
    with new_session() as db:
        rows = db.execute(select(Approval).where(Approval.tool_name == "request_refund")).scalars().all()
        return [a for a in rows if a.arguments.get("order_id") == order_id]


@pytest.mark.parametrize("text", INQUIRIES)
def test_naming_a_refund_is_not_asking_for_one(text):
    assert not refund_requested(text)


@pytest.mark.parametrize("text", REQUESTS)
def test_requests_are_still_requests(text):
    assert refund_requested(text)


@pytest.mark.parametrize("order_id, text", [
    ("SO20260918001", "SO20260918001 退款到账时间"),  # ¥39.90, under the automatic limit: was paid on the spot
    ("SO20261012003", "SO20261012003 退款到账时间"),  # ¥268: went to a reviewer with the question as its reason
    ("SO20260918001", "SO20260918001 when do I get my money back"),  # review P1: paid on the spot
])
def test_asking_when_a_refund_arrives_moves_no_money(ask, login, order_id, text):
    _, events = ask(login("customer@leon.demo"), text)
    tools = [e["name"] for e in by_type(events, "tool_call")]
    final = by_type(events, "final")[0]["text"]
    assert "request_refund" not in tools and not by_type(events, "approval_required")
    assert _refunds(order_id) == 0 and not _applications(order_id)
    assert "到账" in final and f"回复“{order_id} 申请退款”" in final, final


def test_a_bare_refund_word_is_offered_an_application_and_the_reply_files_it(ask, login):
    emma = login("customer@leon.demo")
    conversation, events = ask(emma, "SO20261008010 退款")
    assert not by_type(events, "approval_required") and "申请退款”" in by_type(events, "final")[0]["text"]
    _, events = ask(emma, "申请退款", conversation_id=conversation)  # the reply the offer asks for
    assert by_type(events, "approval_required"), [e["name"] for e in by_type(events, "tool_call")]


def test_chasing_a_refund_that_was_never_filed_says_so(ask, login):
    _, events = ask(login("customer2@leon.demo"), "SO20261015009 我的钱呢 退款呢！")
    assert not by_type(events, "approval_required") and not _applications("SO20261015009")
    assert "没有查到在线提交的退款记录" in by_type(events, "final")[0]["text"]


def test_a_second_application_is_told_the_first_is_waiting(ask, login):
    emma = login("customer@leon.demo")
    _, events = ask(emma, "SO20261012003 我要退款")
    assert by_type(events, "approval_required")
    _, events = ask(emma, "SO20261012003 退款 200 元")
    final = by_type(events, "final")[0]["text"]
    assert "提交的 ¥268.00 退款申请正在等待人工审核，无需重复申请" in final and "未能提交" not in final, final
    assert "如需改成 ¥200.00，可以先回复“撤回退款申请”" in final
    assert len(_applications("SO20261012003")) == 1


def test_a_refund_that_paid_is_not_undone_by_a_later_failure_for_the_same_order():
    paid = Evidence(1, "tool", "request_refund", json.dumps({"refund_id": 7, "amount": 45.0}), {"order_id": "SO1"})
    again = Evidence(2, "tool", "request_refund", json.dumps({"error": "该订单当前状态为“退款中”，不能发起退款"}),
                     {"order_id": "SO1"})
    state = case_state([paid, again], "SO1 音箱坏了 退款")
    assert state.refund == "filed" and render_status(state).refund.startswith("退款单 #7 已受理")
    # another order's failure is still that order's answer
    other = Evidence(3, "tool", "request_refund", json.dumps({"error": "订单不存在或无权查看"}), {"order_id": "SO2"})
    assert case_state([paid, other], "x").refund == "failed"


def test_two_refunds_of_one_order_in_one_step_are_one_refund(ask, login, monkeypatch):
    # SO20260918001 (¥39.90) is paid under the automatic limit: before, the second call ran and failed on "退款中"
    calls = [ToolCall(id="a", name="request_refund", arguments={"order_id": "SO20260918001", "amount": 5}),
             ToolCall(id="b", name="request_refund", arguments={"order_id": "so20260918001 ", "amount": 39.9})]
    monkeypatch.setattr(get_services(), "provider", ScriptedModel(calls, "已为您处理。"))
    _, events = ask(login("customer@leon.demo"), "SO20260918001 抱枕开线了 退5块")
    run_id = by_type(events, "final")[0]["run_id"]
    with new_session() as db:
        spans = db.execute(select(Span).where(Span.run_id == run_id, Span.kind == "tool",
                                              Span.name == "request_refund")).scalars().all()
    assert len(spans) == 1


def test_an_application_for_a_switched_off_tool_cannot_be_approved(client, login, ask, backoffice):  # noqa: F811
    tool = _create(client, login, VOUCHER).json()
    _, events = ask(login("customer@leon.demo"), "快递丢了，给我发 80 元补偿券")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    admin = login("admin@leon.demo")
    r = client.patch(f"/api/admin/tools/{tool['id']}", json={"enabled": False}, headers=admin)
    assert r.json()["pending_approvals"] == [approval_id]
    assert client.get(f"/api/approvals/{approval_id}", headers=admin).json()["tool_off"] is True
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=login("agent@leon.demo"))
    assert r.status_code == 409 and "已停用" in r.json()["detail"]
    with new_session() as db:
        assert db.get(Approval, approval_id).status == "pending"
    assert backoffice.calls("POST", "/v1/vouchers") == []
    assert client.delete(f"/api/admin/tools/{tool['id']}", headers=admin).status_code == 409  # the card names it
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "reject", "comment": "补偿券暂停发放"},
                    headers=login("agent@leon.demo"))
    assert r.status_code == 200 and "补偿券暂停发放" in r.json()["final"]["text"]


@pytest.mark.parametrize("text", ["SO20261012003 能取消订单吗", "SO20261012003 还能取消订单吗？", "SO20261012003 取消订单会扣钱吗"])
def test_asking_about_cancelling_files_nothing(ask, login, text):  # review P1: a question filed ¥268
    _, events = ask(login("customer@leon.demo"), text)
    assert not by_type(events, "approval_required") and not _applications("SO20261012003")
    assert "回复“SO20261012003 帮我取消订单”" in by_type(events, "final")[0]["text"]


def test_a_real_model_cannot_cancel_on_a_question(ask, login, monkeypatch):
    calls = [ToolCall(id="a", name="request_refund", arguments={"order_id": "SO20261012003", "amount": 268})]
    monkeypatch.setattr(get_services(), "provider", ScriptedModel(calls, "好的。"))
    ask(login("customer@leon.demo"), "SO20261012003 能取消订单吗")
    assert not _applications("SO20261012003"), "the precheck refuses: the customer only asked"
