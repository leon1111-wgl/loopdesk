import csv
import io

from tests.conftest import by_type


def _rows(response):
    assert response.status_code == 200, response.text
    assert response.content.startswith("﻿".encode()), "UTF-8 BOM so Excel shows Chinese correctly"
    assert response.headers["content-type"].startswith("text/csv")
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


def test_runs_export_contains_runs_with_tools(client, ask, login):
    ask(login("customer@leon.demo"), "SO20261008002 的快递到哪了？")
    rows = _rows(client.get("/api/export/runs.csv", headers=login("admin@leon.demo")))
    header, data = rows[0], rows[1:]
    assert header[:4] == ["run_id", "created_at", "user", "role"]
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
