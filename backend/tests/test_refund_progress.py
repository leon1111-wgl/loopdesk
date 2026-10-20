"""Asking where a refund is must never file one (N-056): get_order carries the order's refunds and
any request waiting for a person, and the offline model answers from them, read-only."""

import pytest
from sqlalchemy import select

from app.db import new_session
from app.llm.mock import asks_refund_status
from app.models import Approval, Refund
from tests.conftest import by_type


def _tools(events: list[dict]) -> list[str]:
    return [e["name"] for e in by_type(events, "tool_result")]


def _approvals_for(order_id: str) -> list[Approval]:
    with new_session() as db:
        rows = db.execute(select(Approval).where(Approval.tool_name == "request_refund")).scalars().all()
        return [a for a in rows if a.arguments.get("order_id") == order_id]


@pytest.mark.parametrize("text", [
    "SO20261015009 退款到哪一步了", "SO20261012003 退款进度查一下", "SO20261008002 退款怎么样了",
    "SO20261008002 的钱到账了吗", "SO20261008002 退款多久能到账", "SO20261008002 refund status",
])
def test_progress_questions_are_read_only(text):
    assert asks_refund_status(text)


@pytest.mark.parametrize("text", [
    "SO20261008002 我要退款", "SO20261008002 我要退款，多久到账", "SO20261012003 先退我50块吧",
    "SO20261008002 到哪了", "订单 SO20260918001 的抱枕不想要了，申请退款",
])
def test_requests_and_shipment_questions_are_not_progress_questions(text):
    assert not asks_refund_status(text)


def test_asking_progress_without_a_refund_files_nothing(ask, login):
    # Jack has three refunds in the velocity window: filing here would raise a pending approval.
    _, events = ask(login("customer2@leon.demo"), "SO20261015009 退款到哪一步了")
    assert "request_refund" not in _tools(events)
    assert not by_type(events, "approval_required")
    assert _approvals_for("SO20261015009") == []
    text = by_type(events, "final")[0]["text"]
    assert "没有查到在线提交的退款记录" in text and "人工审核" not in text


def test_progress_of_a_request_waiting_for_approval(ask, login):
    emma = login("customer@leon.demo")
    _, first = ask(emma, "SO20261008002 我要退款")
    assert by_type(first, "approval_required")
    _, events = ask(emma, "SO20261008002 退款进度怎么样了")
    assert "request_refund" not in _tools(events) and not by_type(events, "approval_required")
    assert len(_approvals_for("SO20261008002")) == 1, "asking again must not queue a second approval"
    text = by_type(events, "final")[0]["text"]
    assert "¥1299.00" in text and "等待人工审核" in text and "不能重复申请退款" not in text
    order = [e for e in by_type(events, "tool_result") if e["name"] == "get_order"][0]["output"]
    assert "reason" not in (order["refund_awaiting_approval"] or {}), "risk-control reasons stay with the reviewer"


def test_progress_of_a_filed_refund_names_it(ask, login):
    emma = login("customer@leon.demo")
    ask(emma, "SO20261012003 先退我50块吧")
    with new_session() as db:
        refund = db.execute(select(Refund).where(Refund.order_id == "SO20261012003")).scalar_one()
    _, events = ask(emma, "SO20261012003 退款进度查一下")
    assert "request_refund" not in _tools(events)
    text = by_type(events, "final")[0]["text"]
    assert f"退款单 #{refund.id}" in text and "¥50.00" in text and "1-3个工作日" in text
    with new_session() as db:
        assert len(db.execute(select(Refund).where(Refund.order_id == "SO20261012003")).all()) == 1
