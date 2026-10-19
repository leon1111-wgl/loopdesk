"""Failure handling around human-in-the-loop resume: crashes, retries and races."""

import pytest
from sqlalchemy import func, select

from app.agent.tools import ToolContext
from app.db import new_session
from app.models import Approval, Message, Order, Refund, Run, Tenant, User
from app.services import get_services
from tests.conftest import by_type


def _break_model(monkeypatch):
    """Make the next model call crash with a non-LLMError (a bug, a driver error...)."""

    def broken(*args, **kwargs):
        raise RuntimeError("connection reset by upstream")

    monkeypatch.setattr(get_services().provider, "stream", broken)


def _pending_refund(ask, login) -> tuple[dict, int, int]:
    customer = login("customer@leon.demo")
    conversation_id, events = ask(customer, "SO20261008002 我要退款")
    return customer, conversation_id, by_type(events, "approval_required")[0]["approval_id"]


def _refund_count(db) -> int:
    return db.execute(select(func.count(Refund.id)).where(Refund.order_id == "SO20261008002")).scalar_one()


def test_crash_before_the_decision_takes_effect_reopens_the_approval(client, ask, login, monkeypatch):
    customer, conversation_id, approval_id = _pending_refund(ask, login)
    admin = login("admin@leon.demo")
    _break_model(monkeypatch)

    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "reject", "comment": "超期"}, headers=admin)
    assert r.status_code == 503 and "已退回待处理" in r.json()["detail"]
    with new_session() as db:
        approval = db.get(Approval, approval_id)
        run = db.get(Run, approval.run_id)
        assert approval.status == "pending" and approval.reviewer_id is None
        assert run.status == "failed" and "connection reset by upstream" in run.error
        assert db.get(Order, "SO20261008002").status == "shipped"
    messages = client.get(f"/api/conversations/{conversation_id}/messages", headers=customer).json()["messages"]
    assert messages[-1]["meta"]["status"] == "waiting_approval", "the customer is still told to wait"

    monkeypatch.undo()  # the model is back: the same approval can be decided again
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=admin)
    assert r.status_code == 200 and "已受理" in r.json()["final"]["text"]
    with new_session() as db:
        assert db.get(Run, db.get(Approval, approval_id).run_id).status == "completed"
        assert _refund_count(db) == 1


def test_crash_after_the_refund_keeps_the_decision_and_tells_the_customer(client, ask, login, monkeypatch):
    customer, conversation_id, approval_id = _pending_refund(ask, login)
    admin = login("admin@leon.demo")
    _break_model(monkeypatch)  # the refund runs, then writing the answer crashes

    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=admin)
    assert r.status_code == 200
    assert "已受理" in r.json()["final"]["text"] and r.json()["approval"]["status"] == "approved"
    with new_session() as db:
        run = db.get(Run, db.get(Approval, approval_id).run_id)
        assert run.status == "failed" and run.error and run.finished_at is not None
        assert _refund_count(db) == 1
    last = client.get(f"/api/conversations/{conversation_id}/messages", headers=customer).json()["messages"][-1]
    assert "已受理" in last["content"] and last["meta"]["fallback"] is True
    assert client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"},
                       headers=admin).status_code == 409


def test_two_reviewers_deciding_at_once_run_the_tool_once(client, ask, login):
    _, conversation_id, approval_id = _pending_refund(ask, login)
    services = get_services()
    first, second = new_session(), new_session()
    try:
        # Both requests load the approval while it is still pending.
        mine, theirs = first.get(Approval, approval_id), second.get(Approval, approval_id)
        admin = first.execute(select(User).where(User.email == "admin@leon.demo")).scalar_one()
        agent = second.execute(select(User).where(User.email == "agent@leon.demo")).scalar_one()
        services.runtime(first).resume(mine, "approve", admin)
        with pytest.raises(ValueError, match="已处理"):
            services.runtime(second).resume(theirs, "approve", agent)
    finally:
        first.close()
        second.close()
    with new_session() as db:
        assert db.get(Approval, approval_id).reviewer_id == admin.id
        assert _refund_count(db) == 1
        answers = db.execute(select(Message).where(
            Message.conversation_id == conversation_id, Message.role == "assistant")).scalars().all()
        assert len(answers) == 2, "the paused answer plus exactly one resumed answer"


def test_refund_execution_is_idempotent_per_approval(client, ask, login):
    _, _, approval_id = _pending_refund(ask, login)
    services = get_services()
    with new_session() as db:
        approval = db.get(Approval, approval_id)
        run = db.get(Run, approval.run_id)
        ctx = ToolContext(db=db, settings=services.settings, tenant=db.get(Tenant, run.tenant_id),
                          user=db.get(User, run.user_id), embedder=services.embedder, run_id=run.id,
                          conversation_id=run.conversation_id, approval_id=approval.id)
        tool = services.registry.get("request_refund")
        tool.precheck(ctx, approval.arguments)
        first = tool.handler(ctx, approval.arguments)
        tool.precheck(ctx, approval.arguments)  # a retry passes the precheck instead of "退款中"
        second = tool.handler(ctx, approval.arguments)
        assert first["refund_id"] == second["refund_id"] and _refund_count(db) == 1


def test_unexpected_crash_marks_a_streaming_run_failed(ask, login, monkeypatch):
    _break_model(monkeypatch)
    _, events = ask(login("customer@leon.demo"), "退款多久能到账？")
    assert by_type(events, "error")
    with new_session() as db:
        run = db.get(Run, events[0]["run_id"])
        assert run.status == "failed" and "RuntimeError" in run.error


def test_a_failed_feishu_notice_does_not_turn_an_executed_approval_into_an_error(client, ask, login, monkeypatch):
    # The refund is paid and committed before the customer is told on Feishu. A network error
    # there used to surface as a 500, so the reviewer saw a failure for a decision that had
    # taken effect (and a retry answered "已处理").
    import httpx

    from app.channels.feishu import FeishuClient
    from app.models import Conversation

    customer, conversation_id, approval_id = _pending_refund(ask, login)
    with new_session() as db:
        conversation = db.get(Conversation, conversation_id)
        conversation.channel, conversation.external_id = "feishu", "oc_refund"
        db.commit()

    def unreachable(self, chat_id, text):
        raise httpx.ConnectError("feishu unreachable")

    monkeypatch.setattr(FeishuClient, "send_to_chat", unreachable)
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=login("admin@leon.demo"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approval"]["status"] == "approved" and body["delivery"]["sent"] is False
    with new_session() as db:
        assert _refund_count(db) == 1
