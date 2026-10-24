"""The same tool call repeated in one step runs once (N-201).

A model answered "SO20261008010 杯子坏了 退5块钱" with eight identical request_refund calls. The first
went to approval; after Mia approved it and the refund was paid, the other seven ran, failed on
"退款中", and the last of them was what the customer heard: "退款申请未能提交"."""

from sqlalchemy import func, select

from app.db import new_session
from app.llm.base import ToolCall
from app.models import Refund, Span
from app.services import get_services
from tests.conftest import by_type
from tests.scripted_model import ScriptedModel

REFUND = {"order_id": "SO20261008002", "amount": 1299, "reason": "质量问题"}


def _calls(name: str, arguments: dict, n: int) -> list[ToolCall]:
    return [ToolCall(id=f"call_{i}", name=name, arguments=dict(arguments)) for i in range(1, n + 1)]


def _use(monkeypatch, *steps) -> ScriptedModel:
    model = ScriptedModel(*steps)
    monkeypatch.setattr(get_services(), "provider", model)
    return model


def _tool_spans(run_id: str, name: str) -> int:
    with new_session() as db:
        return db.execute(select(func.count(Span.id)).where(
            Span.run_id == run_id, Span.kind == "tool", Span.name == name)).scalar_one()


def test_eight_identical_refunds_file_one_and_the_customer_hears_it_was_paid(client, ask, login, monkeypatch):
    model = _use(monkeypatch, _calls("request_refund", REFUND, 8), "已为您处理。")
    _, events = ask(login("customer@leon.demo"), "SO20261008002 我要退款 屏幕坏了")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    run_id = by_type(events, "final")[0]["run_id"]

    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve", "comment": "ok"},
                    headers=login("admin@leon.demo"))
    text = r.json()["final"]["text"]
    assert r.status_code == 200 and "已受理" in text and "未能提交" not in text
    with new_session() as db:
        assert db.execute(select(func.count(Refund.id)).where(Refund.order_id == "SO20261008002")).scalar_one() == 1
    # One span when it was filed for approval, one when it was carried out; the seven repeats none.
    assert _tool_spans(run_id, "request_refund") == 2
    # Every call still gets its tool message (providers reject an unanswered tool call id).
    answered = [m.tool_call_id for m in model.prompts[-1] if m.role == "tool"]
    assert sorted(answered) == [f"call_{i}" for i in range(1, 9)]


def test_the_repeats_are_in_the_trace(ask, login, monkeypatch):
    _use(monkeypatch, _calls("get_order", {"order_id": "SO20261008002"}, 3), "订单已发货。")
    _, events = ask(login("customer@leon.demo"), "SO20261008002 到哪了")
    run_id = by_type(events, "final")[0]["run_id"]
    assert _tool_spans(run_id, "get_order") == 1
    with new_session() as db:
        span = db.execute(select(Span).where(Span.run_id == run_id, Span.name == "repeated_tool_calls")).scalar_one()
    assert span.output["skipped"] == 2 and span.output["tools"] == ["get_order"]


def test_different_arguments_still_all_run(ask, login, monkeypatch):
    calls = [ToolCall(id="a", name="get_order", arguments={"order_id": "SO20261008002"}),
             ToolCall(id="b", name="get_order", arguments={"order_id": "SO20261008010"})]
    _use(monkeypatch, calls, "两个订单都查到了。")
    _, events = ask(login("customer@leon.demo"), "SO20261008002 和 SO20261008010 到哪了")
    assert _tool_spans(by_type(events, "final")[0]["run_id"], "get_order") == 2


def test_argument_order_does_not_make_a_call_new(ask, login, monkeypatch):
    calls = [ToolCall(id="a", name="request_refund", arguments={"order_id": "SO20261008002", "amount": 1299}),
             ToolCall(id="b", name="request_refund", arguments={"amount": 1299, "order_id": "SO20261008002"})]
    _use(monkeypatch, calls, "已提交。")
    _, events = ask(login("customer@leon.demo"), "SO20261008002 我要退款")
    assert _tool_spans(by_type(events, "final")[0]["run_id"], "request_refund") == 1
