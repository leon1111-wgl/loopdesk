import csv
import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tests.conftest import by_type

BEIJING = ZoneInfo("Asia/Shanghai")


def _rows(response):
    assert response.status_code == 200, response.text
    assert response.content.startswith("﻿".encode()), "UTF-8 BOM so Excel shows Chinese correctly"
    assert response.headers["content-type"].startswith("text/csv")
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


def test_runs_export_contains_runs_with_tools(client, ask, login):
    ask(login("customer@leon.demo"), "SO20261008002 的快递到哪了？")
    rows = _rows(client.get("/api/export/runs.csv", headers=login("admin@leon.demo")))
    header, data = rows[0], rows[1:]
    assert header[:4] == ["run_id", "created_at (Asia/Shanghai)", "user", "role"], "local time, and which zone"
    assert len(data) == 1 and data[0][2] == "Emma" and data[0][-1] == "get_order > track_shipment"


def test_formula_injection_is_neutralised(client, ask, login):
    ask(login("customer@leon.demo"), "=HYPERLINK(\"http://evil.example\",\"点我\")")
    rows = _rows(client.get("/api/export/runs.csv", headers=login("admin@leon.demo")))
    assert rows[1][4].startswith("'="), "user text must not run as a spreadsheet formula"


def test_feedback_and_approvals_exports(client, ask, login):
    customer = login("customer@leon.demo")
    _, events = ask(customer, "SO20261008002 我要退款")
    message_id = by_type(events, "final")[0]["message_id"]
    client.post(f"/api/messages/{message_id}/feedback", json={"rating": "down"}, headers=customer)
    admin = login("admin@leon.demo")
    approvals = _rows(client.get("/api/export/approvals.csv", headers=admin))
    assert approvals[1][4] == "SO20261008002" and approvals[1][7] == "pending"
    feedback = _rows(client.get("/api/export/feedback.csv", headers=admin))
    assert feedback[1][3] == "down"


def test_exports_are_staff_only_and_tenant_scoped(client, ask, login):
    ask(login("customer@leon.demo"), "退款多久能到账？")
    assert client.get("/api/export/runs.csv", headers=login("customer@leon.demo")).status_code == 403
    iris = _rows(client.get("/api/export/runs.csv", headers=login("admin@iris.demo")))
    assert iris[1:] == [], "another tenant's runs never appear"
    assert client.get("/api/export/runs.csv?days=0", headers=login("admin@leon.demo")).status_code == 400


# Review round 3, N-023: only runs, feedback and approvals could be exported, all in UTC. Operations
# also need the conversations themselves, the tickets and the handoff records, in local time.

def _local(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING)


def test_the_conversation_export_has_every_message_and_who_wrote_it(client, ask, login):
    jack, mia, admin = login("customer2@leon.demo"), login("agent@leon.demo"), login("admin@leon.demo")
    conversation_id, _ = ask(jack, "转人工")
    handoff_id = client.get("/api/handoffs", headers=mia).json()[0]["ticket_id"]
    client.post(f"/api/handoffs/{handoff_id}/reply", json={"content": "您好，我是 Mia"}, headers=mia)
    rows = _rows(client.get("/api/export/conversations.csv?days=7", headers=admin))
    header, data = rows[0], [dict(zip(rows[0], r)) for r in rows[1:]]
    assert "created_at (Asia/Shanghai)" in header
    mine = [r for r in data if r["conversation_id"] == str(conversation_id)]
    assert [(r["role"], r["author"]) for r in mine] == [("user", "Jack"), ("assistant", ""), ("agent", "Mia")]
    assert mine[-1]["content"] == "您好，我是 Mia"
    assert abs(_local(mine[-1]["created_at (Asia/Shanghai)"]) - datetime.now(timezone.utc)) < timedelta(minutes=2)


def test_ticket_and_handoff_exports_carry_the_service_levels(client, ask, login):
    emma, jack = login("customer@leon.demo"), login("customer2@leon.demo")
    mia, admin = login("agent@leon.demo"), login("admin@leon.demo")
    ask(emma, "我买的扫地机器人坏了开不了机，马上给我处理！")
    ticket_id = client.get("/api/tickets", headers=mia).json()[0]["id"]
    client.post(f"/api/tickets/{ticket_id}/close", json={"resolution": "已安排上门检修"}, headers=mia)
    tickets = _rows(client.get("/api/export/tickets.csv", headers=admin))
    ticket = dict(zip(tickets[0], tickets[1]))
    assert ticket["ticket_id"] == str(ticket_id) and ticket["sla"] == "met" and ticket["assignee"] == "Mia"
    assert ticket["resolution"] == "已安排上门检修" and ticket["priority"] in ("high", "urgent")
    assert _local(ticket["due_at (Asia/Shanghai)"]) - _local(ticket["created_at (Asia/Shanghai)"]) == timedelta(hours=4)

    conversation_id, _ = ask(jack, "转人工")
    handoff_id = client.get("/api/handoffs", headers=mia).json()[0]["ticket_id"]
    client.post(f"/api/handoffs/{handoff_id}/reply", json={"content": "您好"}, headers=mia)
    client.post(f"/api/handoffs/{handoff_id}/close", headers=mia)
    handoffs = _rows(client.get("/api/export/handoffs.csv", headers=admin))
    handoff = dict(zip(handoffs[0], handoffs[1]))
    assert handoff["handoff_id"] == str(handoff_id) and handoff["conversation_id"] == str(conversation_id)
    assert handoff["assignee"] == "Mia" and handoff["status"] == "closed" and handoff["customer"] == "Jack"
    assert handoff["first_response_at (Asia/Shanghai)"] and 0 <= int(handoff["wait_seconds"]) < 60
    assert handoff["closed_at (Asia/Shanghai)"]


def test_the_new_exports_are_staff_only_and_tenant_scoped(client, ask, login):
    ask(login("customer@leon.demo"), "我买的扫地机器人坏了开不了机，马上给我处理！")
    for kind in ("conversations", "tickets", "handoffs"):
        assert client.get(f"/api/export/{kind}.csv", headers=login("customer@leon.demo")).status_code == 403
        assert _rows(client.get(f"/api/export/{kind}.csv", headers=login("admin@iris.demo")))[1:] == []
    assert len(_rows(client.get("/api/export/tickets.csv", headers=login("admin@leon.demo")))) == 2


def test_first_response_counts_only_replies_while_that_handoff_was_open(client, ask, login):
    # Found while adding the handoff export: a handoff ended without a reply took the next handoff's
    # reply as its own first response, an hour later, and the dashboard average with it.
    from app.db import new_session
    from app.models import Ticket

    jack, mia, admin = login("customer2@leon.demo"), login("agent@leon.demo"), login("admin@leon.demo")
    conversation_id, _ = ask(jack, "转人工")
    first = client.get("/api/handoffs", headers=mia).json()[0]["ticket_id"]
    client.post(f"/api/handoffs/{first}/close", headers=mia)  # ended without a word
    with new_session() as db:  # an hour ago
        ticket = db.get(Ticket, first)
        ticket.created_at = datetime.now(timezone.utc) - timedelta(hours=1, minutes=5)
        ticket.closed_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
    ask(jack, "还是转人工吧", conversation_id=conversation_id)
    second = client.get("/api/handoffs", headers=mia).json()[0]["ticket_id"]
    client.post(f"/api/handoffs/{second}/reply", json={"content": "您好"}, headers=mia)
    queue = client.get("/api/metrics/overview", headers=admin).json()["handoff_queue"]
    assert queue["handoffs"] == 2 and queue["avg_first_response_s"] < 60
    rows = {r[0]: r for r in _rows(client.get("/api/export/handoffs.csv", headers=admin))[1:]}
    assert rows[str(first)][6] == "" and int(rows[str(second)][6]) < 60, "wait_seconds"
