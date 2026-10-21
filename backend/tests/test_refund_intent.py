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
])
def test_requests_still_count(text):
    assert refund_requested(text) and not refund_declined(text)


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
