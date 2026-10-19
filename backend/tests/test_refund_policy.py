"""The approval policy must judge the refund that will actually be executed, whatever
shape the model's arguments take (real models omit optional fields and vary casing)."""

from sqlalchemy import select

from app.db import new_session
from app.llm.base import ChatResult, LLMProvider, ToolCall, Usage
from app.models import Approval, Order, Refund
from app.services import get_services
from tests.conftest import by_type


class ScriptedRefundModel(LLMProvider):
    """Calls request_refund once with fixed arguments, then answers in plain text."""

    name = "scripted"

    def __init__(self, arguments: dict):
        self.arguments = arguments

    def chat(self, messages, tools, model, options=None) -> ChatResult:
        if messages[-1].role == "user":
            call = ToolCall(id="call_1", name="request_refund", arguments=dict(self.arguments))
            return ChatResult(content="", tool_calls=[call], usage=Usage(), model=model, finish_reason="tool_calls")
        return ChatResult(content="已为您处理。", tool_calls=[], usage=Usage(), model=model)


def _use_model(monkeypatch, arguments: dict) -> None:
    monkeypatch.setattr(get_services(), "provider", ScriptedRefundModel(arguments))


def test_refund_without_amount_needs_approval_for_the_full_order_amount(ask, login, monkeypatch):
    # The schema makes "amount" optional ("默认订单实付金额"); the policy used to read it as 0.
    _use_model(monkeypatch, {"order_id": "so20261008002", "reason": "质量问题"})
    _, events = ask(login("customer@leon.demo"), "SO20261008002 我要退款")

    approval = by_type(events, "approval_required")[0]
    assert approval["arguments"]["amount"] == 1299.0 and approval["arguments"]["order_id"] == "SO20261008002"
    assert "¥1299.00" in by_type(events, "final")[0]["text"]
    with new_session() as db:
        assert db.get(Order, "SO20261008002").status == "shipped", "no money moves before approval"
        assert db.execute(select(Refund).where(Refund.order_id == "SO20261008002")).first() is None
        stored = db.get(Approval, approval["approval_id"])
        assert stored.arguments["amount"] == 1299.0, "the reviewer sees the amount that will be paid"


def test_zero_amount_is_rejected_instead_of_becoming_a_full_refund(ask, login, monkeypatch):
    _use_model(monkeypatch, {"order_id": "SO20261008002", "amount": 0, "reason": "质量问题"})
    _, events = ask(login("customer@leon.demo"), "SO20261008002 我要退款")
    result = [e for e in by_type(events, "tool_result") if e["name"] == "request_refund"][0]
    assert result["output"]["error"] == "退款金额必须大于 0"
    assert not by_type(events, "approval_required")
    with new_session() as db:
        assert db.get(Order, "SO20261008002").status == "shipped"


def test_non_finite_amount_is_rejected(ask, login, monkeypatch):
    _use_model(monkeypatch, {"order_id": "SO20261008002", "amount": "NaN", "reason": "质量问题"})
    _, events = ask(login("customer@leon.demo"), "SO20261008002 我要退款")
    result = [e for e in by_type(events, "tool_result") if e["name"] == "request_refund"][0]
    assert result["output"]["error"] == "退款金额格式不正确"
    with new_session() as db:
        assert db.get(Order, "SO20261008002").status == "shipped"
        assert db.execute(select(Refund).where(Refund.order_id == "SO20261008002")).first() is None


def test_pending_duplicate_is_detected_regardless_of_order_id_casing(ask, login, monkeypatch):
    customer = login("customer@leon.demo")
    _use_model(monkeypatch, {"order_id": "so20261008002", "amount": 1299, "reason": "质量问题"})
    _, first = ask(customer, "SO20261008002 我要退款")
    assert by_type(first, "approval_required")
    _use_model(monkeypatch, {"order_id": "SO20261008002", "amount": 1299, "reason": "质量问题"})
    _, second = ask(customer, "SO20261008002 我要退款")
    result = [e for e in by_type(second, "tool_result") if e["name"] == "request_refund"][0]
    assert "等待人工审核" in result["output"]["error"]
