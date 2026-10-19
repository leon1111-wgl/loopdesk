"""Human takeover: one ticket per conversation, the agent stays quiet, staff reply and hand back."""

from sqlalchemy import func, select

from app.db import new_session
from app.models import Run, Ticket
from tests.conftest import by_type


def _handoffs(db, conversation_id=None):
    query = select(func.count(Ticket.id)).where(Ticket.kind == "handoff")
    if conversation_id is not None:
        query = query.where(Ticket.conversation_id == conversation_id)
    return db.execute(query).scalar_one()


def test_asking_again_does_not_queue_the_customer_twice(ask, login):
    jack = login("customer2@leon.demo")
    conversation_id, first = ask(jack, "转人工")
    assert by_type(first, "final")[0]["status"] == "handoff"
    with new_session() as db:
        runs_before = db.execute(select(func.count(Run.id))).scalar_one()
    _, again = ask(jack, "转人工！！！快点", conversation_id=conversation_id)
    assert not by_type(again, "tool_call"), "a person owns the conversation: no agent run"
    assert "当前排队第 1 位" in by_type(again, "final")[0]["text"], "his own ticket is not counted ahead of him"
    with new_session() as db:
        assert _handoffs(db, conversation_id) == 1
        assert db.execute(select(func.count(Run.id))).scalar_one() == runs_before, "no run, no handoff-rate inflation"


def test_queue_position_counts_only_earlier_waiting_customers(ask, login):
    ask(login("customer@leon.demo"), "转人工")
    _, events = ask(login("customer2@leon.demo"), "我要投诉，转人工")
    assert "当前排队第 2 位" in by_type(events, "final")[0]["text"]


def test_staff_reply_reaches_the_customer_and_hand_back_restores_the_agent(client, ask, login):
    emma, mia = login("customer@leon.demo"), login("agent@leon.demo")
    conversation_id, _ = ask(emma, "转人工")
    queue = client.get("/api/handoffs", headers=mia).json()
    assert [q["conversation_id"] for q in queue] == [conversation_id] and queue[0]["position"] == 1
    ticket_id = queue[0]["ticket_id"]

    r = client.post(f"/api/handoffs/{ticket_id}/reply", json={"content": "您好，我是 Mia，请问有什么可以帮您？"}, headers=mia)
    assert r.status_code == 200 and r.json()["handoff"]["assignee"] == "Mia"
    _, events = ask(emma, "我的扫地机器人还没到", conversation_id=conversation_id)
    assert "人工客服 Mia 正在为您服务" in by_type(events, "final")[0]["text"]
    page = client.get(f"/api/conversations/{conversation_id}/messages", headers=emma).json()
    assert page["handoff"]["assignee"] == "Mia"
    assert [m["role"] for m in page["messages"]] == ["user", "assistant", "agent", "user"]

    assert client.post(f"/api/handoffs/{ticket_id}/close", headers=mia).status_code == 200
    assert client.get("/api/handoffs", headers=mia).json() == []
    _, events = ask(emma, "退款多久能到账？", conversation_id=conversation_id)
    assert [e["name"] for e in by_type(events, "tool_call")] == ["search_knowledge"], "the agent answers again"
    assert client.post(f"/api/handoffs/{ticket_id}/reply", json={"content": "还在吗"}, headers=mia).status_code == 409


def test_handoff_queue_is_staff_only_and_tenant_scoped(client, ask, login):
    ask(login("customer@leon.demo"), "转人工")
    assert client.get("/api/handoffs", headers=login("customer@leon.demo")).status_code == 403
    assert client.get("/api/handoffs", headers=login("admin@iris.demo")).json() == []
    with new_session() as db:
        ticket_id = db.execute(select(Ticket.id).where(Ticket.kind == "handoff")).scalar_one()
    r = client.post(f"/api/handoffs/{ticket_id}/reply", json={"content": "hi"}, headers=login("admin@iris.demo"))
    assert r.status_code == 404


def test_human_replies_reach_the_model_as_assistant_turns(client, ask, login):
    emma, mia = login("customer@leon.demo"), login("agent@leon.demo")
    conversation_id, _ = ask(emma, "转人工")
    ticket_id = client.get("/api/handoffs", headers=mia).json()[0]["ticket_id"]
    client.post(f"/api/handoffs/{ticket_id}/reply", json={"content": "已帮您登记"}, headers=mia)
    client.post(f"/api/handoffs/{ticket_id}/close", headers=mia)
    from app.agent.runtime import AgentRuntime  # noqa: F401  (history is built by the runtime)
    from app.models import Conversation
    from app.services import get_services

    with new_session() as db:
        history = get_services().runtime(db)._history(db.get(Conversation, conversation_id))
    assert {m.role for m in history} <= {"user", "assistant"}, "model APIs reject other roles"
    assert any(m.content == "（人工客服）已帮您登记" for m in history)


def test_the_tool_itself_reuses_an_open_ticket(settings, ask, login):
    # The runtime keeps the agent out of a conversation that is with a person; if the tool is
    # reached anyway (another entry point, a race), it still must not queue the customer twice.
    from app.agent.business_tools import handoff_to_human
    from app.agent.tools import ToolContext
    from app.models import Tenant, User

    conversation_id, _ = ask(login("customer@leon.demo"), "转人工")
    with new_session() as db:
        emma = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
        ctx = ToolContext(db=db, settings=settings, tenant=db.get(Tenant, emma.tenant_id), user=emma,
                          embedder=None, run_id="test", conversation_id=conversation_id)
        out = handoff_to_human(ctx, {"reason": "再次要求"})
        assert out["already_queued"] is True and out["queue_position"] == 1 and "无需重复" in out["message"]
        assert _handoffs(db, conversation_id) == 1


def test_concurrent_handoffs_cannot_open_two_tickets(settings, ask, login, monkeypatch):
    # Simulate the race: the check runs before the other request's insert is visible.
    from app.agent import business_tools, handoff
    from app.agent.tools import ToolContext
    from app.models import Tenant, User

    conversation_id, _ = ask(login("customer@leon.demo"), "转人工")
    real_lookup = handoff.active_ticket
    calls = []

    def stale_lookup(db, cid):
        calls.append(cid)
        return None if len(calls) == 1 else real_lookup(db, cid)

    monkeypatch.setattr(handoff, "active_ticket", stale_lookup)
    with new_session() as db:
        emma = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
        ctx = ToolContext(db=db, settings=settings, tenant=db.get(Tenant, emma.tenant_id), user=emma,
                          embedder=None, run_id="test", conversation_id=conversation_id)
        out = business_tools.handoff_to_human(ctx, {"reason": "并发的第二次请求"})
        assert out["already_queued"] is True, "the unique index rejected the second insert"
        assert _handoffs(db, conversation_id) == 1


def test_dashboard_reports_the_queue_and_first_response(client, ask, login):
    emma, mia, admin = login("customer@leon.demo"), login("agent@leon.demo"), login("admin@leon.demo")
    ask(login("customer2@leon.demo"), "转人工")
    ask(emma, "转人工")
    queue = client.get("/api/metrics/overview", headers=admin).json()["handoff_queue"]
    assert queue["waiting"] == 2 and queue["avg_first_response_s"] is None
    ticket_id = client.get("/api/handoffs", headers=mia).json()[1]["ticket_id"]
    client.post(f"/api/handoffs/{ticket_id}/reply", json={"content": "您好"}, headers=mia)
    queue = client.get("/api/metrics/overview", headers=admin).json()["handoff_queue"]
    assert (queue["waiting"], queue["in_progress"], queue["handoffs"]) == (1, 1, 2)
    assert queue["avg_first_response_s"] is not None and queue["longest_wait_s"] >= 0
