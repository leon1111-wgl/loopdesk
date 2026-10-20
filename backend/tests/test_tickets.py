"""Issue tickets the agent opens for customers (review round 3, N-019)."""

from datetime import datetime, timedelta, timezone

from tests.conftest import by_type

BROKEN = "我买的扫地机器人坏了开不了机，马上给我处理！"


def _open_ticket(ask, login):
    customer = login("customer@leon.demo")
    conversation_id, events = ask(customer, BROKEN)
    assert "工单" in by_type(events, "final")[0]["text"]
    return customer, conversation_id


def test_staff_see_the_tickets_the_agent_promised_a_response_on(client, ask, login):
    # N-019: the agent told the customer "技术支持将在 4 小时内联系您", but no page, API or export
    # showed issue tickets to anyone, so nobody could keep the promise.
    _open_ticket(ask, login)
    tickets = client.get("/api/tickets", headers=login("agent@leon.demo")).json()
    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket["status"] == "open" and ticket["priority"] in ("high", "urgent")
    due = datetime.fromisoformat(ticket["due_at"])
    created = datetime.fromisoformat(ticket["created_at"])
    assert timedelta(hours=3, minutes=59) < due - created <= timedelta(hours=4, minutes=1), "the 4 hours it promised"
    assert ticket["overdue"] is False and ticket["customer"]["name"]
    assert client.get("/api/tickets", headers=login("admin@iris.demo")).json() == [], "per tenant"


def test_a_ticket_is_claimed_once_and_closed_with_a_resolution_the_customer_sees(client, ask, login):
    customer, conversation_id = _open_ticket(ask, login)
    agent, admin = login("agent@leon.demo"), login("admin@leon.demo")
    ticket_id = client.get("/api/tickets", headers=agent).json()[0]["id"]
    claimed = client.post(f"/api/tickets/{ticket_id}/claim", headers=agent).json()
    assert claimed["status"] == "in_progress" and claimed["assignee"]["name"]
    assert client.post(f"/api/tickets/{ticket_id}/claim", headers=admin).status_code == 409
    assert client.post(f"/api/tickets/{ticket_id}/close", json={"resolution": " "}, headers=agent).status_code == 400
    closed = client.post(f"/api/tickets/{ticket_id}/close", json={"resolution": "已安排上门检修，明天上午到达"},
                         headers=agent).json()
    assert closed["status"] == "closed"
    messages = client.get(f"/api/conversations/{conversation_id}/messages", headers=customer).json()["messages"]
    assert "已安排上门检修" in messages[-1]["content"] and f"#{ticket_id}" in messages[-1]["content"]
    assert client.get("/api/tickets", headers=agent).json() == [], "closed tickets leave the default list"
    assert len(client.get("/api/tickets?status_filter=all", headers=agent).json()) == 1


def test_overdue_tickets_are_flagged_and_counted_on_the_dashboard(client, ask, login):
    from sqlalchemy import update

    from app.db import new_session
    from app.models import Ticket

    _open_ticket(ask, login)
    admin = login("admin@leon.demo")
    with new_session() as db:  # five hours later
        past = datetime.now(timezone.utc) - timedelta(hours=5)
        db.execute(update(Ticket).where(Ticket.kind == "issue").values(created_at=past, due_at=past + timedelta(hours=4)))
        db.commit()
    assert client.get("/api/tickets", headers=admin).json()[0]["overdue"] is True
    backlog = client.get("/api/metrics/overview", headers=admin).json()["tickets"]
    assert backlog == {"open": 1, "overdue": 1}


def test_closing_an_unclaimed_ticket_records_who_handled_it(client, ask, login):
    _open_ticket(ask, login)
    agent = login("agent@leon.demo")
    ticket_id = client.get("/api/tickets", headers=agent).json()[0]["id"]
    closed = client.post(f"/api/tickets/{ticket_id}/close", json={"resolution": "电话沟通后已远程恢复"},
                         headers=agent).json()
    assert closed["assignee"] and closed["assignee"]["name"] == "Mia"


def test_the_full_list_keeps_open_tickets_ahead_of_a_long_closed_history(client, ask, login):
    # The list was capped with no ORDER BY, so once a tenant had a few hundred closed tickets
    # the open ones (the only ones anyone has to act on) could fall past the cap.
    from app.db import new_session
    from app.models import Ticket, User

    with new_session() as db:
        emma = db.query(User).filter_by(email="customer@leon.demo").one()
        start = datetime.now(timezone.utc) - timedelta(days=30)
        db.add_all(Ticket(tenant_id=emma.tenant_id, customer_id=emma.id, title=f"历史工单 {i}", status="closed",
                          created_at=start, closed_at=start + timedelta(minutes=i), resolution="已处理")
                   for i in range(600))
        db.commit()
    _open_ticket(ask, login)
    rows = client.get("/api/tickets?status_filter=all", headers=login("agent@leon.demo")).json()
    assert rows[0]["status"] == "open", "the open ticket comes first, whatever the history"
    closed = [r for r in rows if r["status"] == "closed"]
    assert closed[0]["title"] == "历史工单 599", "the most recently closed history follows"
    assert len(closed) == 100
