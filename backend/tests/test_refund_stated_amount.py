"""The amount a customer names is the amount filed (N-218). "退我20就行" used to become the full ¥45 and
"退我500" the ¥32 paid, and the reply never said so. The offline model now reads the bare number, and the
tool refuses a call whose amount differs from the one the customer just named, whatever a real model fills in."""

import pytest
from sqlalchemy import select

from app.db import new_session
from app.models import Approval, Refund
from app.services import get_services
from app.textutil import stated_refund_amount
from tests.conftest import by_type
from tests.test_refund_policy import ScriptedRefundModel

JACK, EMMA = "customer2@leon.demo", "customer@leon.demo"


@pytest.mark.parametrize("text, amount", [
    ("SO20261015009 音箱坏了 退我20就行", 20.0), ("SO20261008010 退我500", 500.0), ("退给我 30.5 吧", 30.5),
    ("给我退20", 20.0), ("退我¥20", 20.0), ("退我20块", 20.0), ("899 块买的 退 100 块", 100.0), ("只退 50 元就行", 50.0),
    ("我要退款", None), ("退一半", None), ("退我20%", None), ("退我2件", None), ("不要退100了 我要退款", None),
    # Found by review: spaced counters, separators, earlier refunds, prices, times and order numbers.
    ("退我 2 件", None), ("给我退 3 个", None), ("退我 20 %", None), ("退我1,299", 1299.0), ("退我 1,299 元就行", 1299.0),
    ("退我 3 次了都没到账", None), ("上次退我 20 的那个还没到账 这次我要全退", None), ("退我45元买的音箱 退一半就行", None),
    ("退我 2 天内到账吗", None), ("退我20261015009", None), ("退我 0", None), ("退我20，谢谢", 20.0),
])
def test_stated_refund_amount(text, amount):
    assert stated_refund_amount(text) == amount


def _refund_events(events):
    return [e for e in by_type(events, "tool_result") if e["name"] == "request_refund"]


def _filed(order_id: str) -> list[float]:
    with new_session() as db:
        refunds = [r.amount for r in db.execute(select(Refund).where(Refund.order_id == order_id)).scalars()]
        approvals = [a.arguments.get("amount") for a in db.execute(select(Approval).where(
            Approval.tool_name == "request_refund")).scalars() if a.arguments.get("order_id") == order_id]
        return refunds + approvals


def test_offline_model_files_the_bare_amount(ask, login):
    _, events = ask(login(JACK), "SO20261015009 音箱坏了 退我20就行")
    assert _filed("SO20261015009") == [20.0]
    assert "45" not in by_type(events, "final")[0]["text"]


def test_more_than_paid_says_the_most_that_can_be_refunded(ask, login):
    _, events = ask(login(EMMA), "SO20261008010 退我500")
    assert "最多可退 ¥32.00" in _refund_events(events)[0]["output"]["error"]
    assert "最多可退 ¥32.00" in by_type(events, "final")[0]["text"]
    assert _filed("SO20261008010") == []


@pytest.mark.parametrize("login_as, order_id, text, arguments, says", [
    # A model that files the full order when the customer asked for part of it.
    (JACK, "SO20261015009", "SO20261015009 音箱坏了 退我20就行", {"amount": 45.0}, "金额不一致"),
    (JACK, "SO20261015009", "SO20261015009 音箱坏了 退我20就行", {}, "金额不一致"),  # amount omitted = full order
    # A model that caps "退我500" at what was paid without asking.
    (EMMA, "SO20261008010", "SO20261008010 退我500", {"amount": 32.0}, "请先和顾客确认"),
])
def test_tool_refuses_an_amount_the_customer_did_not_ask_for(ask, login, monkeypatch, login_as, order_id, text,
                                                             arguments, says):
    monkeypatch.setattr(get_services(), "provider",
                        ScriptedRefundModel({"order_id": order_id, "reason": "质量问题", **arguments}))
    _, events = ask(login(login_as), text)
    assert says in _refund_events(events)[0]["output"]["error"]
    assert not by_type(events, "approval_required")
    assert _filed(order_id) == []


def test_two_orders_in_one_message_are_not_compared(ask, login, monkeypatch):
    # "退我20" may belong to the other order: the comparison is skipped rather than refusing the full refund.
    monkeypatch.setattr(get_services(), "provider",
                        ScriptedRefundModel({"order_id": "SO20261008010", "amount": 32.0, "reason": "质量问题"}))
    _, events = ask(login(EMMA), "SO20261008002 退我20，SO20261008010 全退")
    assert all("金额不一致" not in str(e["output"]) for e in _refund_events(events))
    assert _filed("SO20261008010") == [32.0]


def test_tool_files_the_amount_the_customer_named(ask, login, monkeypatch):
    monkeypatch.setattr(get_services(), "provider",
                        ScriptedRefundModel({"order_id": "SO20261015009", "amount": 20, "reason": "质量问题"}))
    _, events = ask(login(JACK), "SO20261015009 音箱坏了 退我20就行")
    assert all("error" not in e["output"] for e in _refund_events(events))
    assert _filed("SO20261015009") == [20.0]
