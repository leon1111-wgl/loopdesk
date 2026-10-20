"""The refund console: staff see the refunds the payment system has not settled, next to the
administrator's settle action (N-028 follow-up ③). Before it, a stuck refund reached people only as
a ticket, and recording its outcome took a curl call."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import CommerceApi
from app.db import new_session
from app.integrations import Commerce
from app.integrations import http as commerce_http
from app.models import Refund, Ticket, User
from app.services import get_services
from tests.conftest import by_type
from tests.fake_commerce import TOKEN, FakeCommerce

BASE = "https://oms.leon.test/loopdesk"
FIELDS = {"id", "order_id", "amount", "status", "attempts", "error", "external_id", "idempotency_key", "ticket_id",
          "approval_id", "conversation_id", "customer", "created_at", "updated_at", "follow_up", "resend_until"}


@pytest.fixture()
def leon_over_http(settings, client):
    """Leon优选's payments go through its own payment system (no call is made unless a test asks)."""
    settings.commerce_apis = {"leon": CommerceApi(base_url=BASE, token=TOKEN)}
    get_services().commerce = Commerce(settings, transport=FakeCommerce().transport)


def _add(email: str, order_id: str, status: str, hours_ago: float = 0.0, **values) -> int:
    with new_session() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one()
        refund = Refund(tenant_id=user.tenant_id, order_id=order_id, customer_id=user.id,
                        amount=values.pop("amount", 59.0), status=status,
                        idempotency_key=values.pop("idempotency_key", uuid.uuid4().hex),
                        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago), **values)
        db.add(refund)
        db.commit()
        return refund.id


def _list(client, headers, query: str = "") -> list[dict]:
    r = client.get(f"/api/refunds{query}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_staff_see_the_unsettled_refunds(client, login, leon_over_http):
    pending = _add("customer@leon.demo", "SO20261020201", "pending", hours_ago=3, attempts=4, error="timeout")
    processing = _add("customer2@leon.demo", "SO20261020202", "processing", hours_ago=1, attempts=1,
                      external_id="RF000202")
    _add("customer@leon.demo", "SO20261020203", "succeeded", hours_ago=0.5, external_id="RF000203")
    _add("customer@leon.demo", "SO20261020204", "failed", hours_ago=0.2, error="order_not_refundable")

    rows = _list(client, login("agent@leon.demo"))
    mine = {r["id"]: r for r in rows}
    assert {pending, processing} <= set(mine), "both are waiting for the payment system"
    assert not {r["status"] for r in rows} - {"pending", "processing"}, "settled refunds are not listed"
    waiting = [r for r in rows if r["follow_up"] == "person"]  # Jack's seeded ones: nobody follows them
    assert rows[:len(waiting)] == waiting, "the ones waiting for a person first"
    created = [r["created_at"] for r in rows[len(waiting):]]
    assert created == sorted(created, reverse=True), "then newest first"
    assert [r for r in rows if r["id"] in (pending, processing)][0]["id"] == processing

    row = mine[pending]
    assert set(row) == FIELDS
    assert (row["order_id"], row["amount"], row["attempts"], row["error"]) == ("SO20261020201", 59.0, 4, "timeout")
    assert row["customer"]["name"] == "Emma" and row["external_id"] is None
    with new_session() as db:
        assert row["idempotency_key"] == db.get(Refund, pending).idempotency_key, "what the back office searches by"
    assert row["follow_up"] == "resend", "the reconciliation job still sends it again"
    resend_until = datetime.fromisoformat(row["resend_until"]) - datetime.fromisoformat(row["created_at"])
    assert resend_until == timedelta(hours=24)
    assert (mine[processing]["follow_up"], mine[processing]["resend_until"]) == ("follow", None)


def test_filters(client, login, leon_over_http):
    ids = {status: _add("customer@leon.demo", f"SO2026102021{i}", status, hours_ago=i)
           for i, status in enumerate(("pending", "processing", "succeeded", "failed"))}
    admin = login("admin@leon.demo")
    for status in ("pending", "processing", "succeeded", "failed"):
        rows = _list(client, admin, f"?status={status}")
        assert ids[status] in {r["id"] for r in rows} and {r["status"] for r in rows} == {status}, status
    assert set(ids.values()) <= {r["id"] for r in _list(client, admin, "?status=all")}
    assert {r["id"] for r in _list(client, admin)} == {r["id"] for r in _list(client, admin, "?status=open")}
    assert client.get("/api/refunds?status=refunded", headers=admin).status_code == 422


def test_what_happens_next_follows_the_reconciliation_job(client, login, leon_over_http):
    stale = _add("customer@leon.demo", "SO20261020221", "pending", hours_ago=25)
    untracked = _add("customer@leon.demo", "SO20261020222", "processing", hours_ago=2)  # no payment-system id
    old = _add("customer@leon.demo", "SO20261020223", "processing", hours_ago=31 * 24, external_id="RF000223")
    rows = {r["id"]: r for r in _list(client, login("admin@leon.demo"))}
    assert rows[stale]["follow_up"] == "person", "past 24 hours it is not sent again: a person settles it"
    assert rows[untracked]["follow_up"] == "person" and rows[old]["follow_up"] == "person", "nobody follows these"


def test_refunds_in_the_demo_tables_do_not_wait_for_a_payment_system(client, login):
    # Money does not move in the demo tables: their refunds stay "processing" for good (Jack's three
    # seeded ones too). They are not unsettled refunds, so they are not on the console's default list.
    pending = _add("customer@leon.demo", "SO20261020231", "pending", hours_ago=1)
    agent = login("agent@leon.demo")
    assert [r["id"] for r in _list(client, agent)] == [pending]
    demo = _list(client, agent, "?status=processing")
    assert len(demo) == 3 and {r["follow_up"] for r in demo} == {"demo"}
    assert {r["customer"]["name"] for r in demo} == {"Jack"}


def test_each_tenant_sees_only_its_own_refunds(client, login):
    leon = _add("customer@leon.demo", "SO20261020241", "pending")
    echo = _add("customer@echo.demo", "SO20261020242", "pending")
    assert {r["id"] for r in _list(client, login("admin@leon.demo"), "?status=all")} >= {leon}
    assert echo not in {r["id"] for r in _list(client, login("admin@leon.demo"), "?status=all")}
    assert [r["id"] for r in _list(client, login("agent@echo.demo"), "?status=all")] == [echo]
    assert _list(client, login("admin@iris.demo"), "?status=all") == []
    r = client.post(f"/api/refunds/{echo}/settle", headers=login("admin@leon.demo"),
                    json={"outcome": "succeeded", "note": "跨租户确认"})
    assert r.status_code == 404, "another tenant's refund does not exist here"


def test_only_staff_see_the_refund_ledger(client, login):
    _add("customer@leon.demo", "SO20261020251", "pending")
    assert client.get("/api/refunds").status_code == 401
    assert client.get("/api/refunds", headers=login("customer@leon.demo")).status_code == 403
    assert client.get("/api/refunds", headers=login("agent@leon.demo")).status_code == 200
    assert client.get("/api/refunds", headers=login("admin@leon.demo")).status_code == 200


def test_at_most_200_refunds_newest_first(client, login):
    with new_session() as db:
        emma = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
        start = datetime.now(timezone.utc) - timedelta(hours=10)
        db.add_all(Refund(tenant_id=emma.tenant_id, order_id=f"SO2026102{i:05d}", customer_id=emma.id, amount=1.0,
                          status="pending", idempotency_key=uuid.uuid4().hex, created_at=start + timedelta(minutes=i))
                   for i in range(205))
        db.commit()
    rows = _list(client, login("agent@leon.demo"))
    assert len(rows) == 200
    assert rows[0]["order_id"] == "SO202610200204" and rows[-1]["order_id"] == "SO202610200005"


def test_a_refund_the_payment_system_never_answered_is_listed_and_settled_from_the_console(
        settings, client, ask, login, monkeypatch):
    monkeypatch.setattr(commerce_http, "_sleep", lambda seconds: None)
    fake = FakeCommerce()
    fake.add_order("SO20261020101", "customer@leon.demo", 39.0, items=[{"name": "远程云朵抱枕", "qty": 1, "price": 39.0}])
    fake.fail("POST", "/loopdesk/refunds", status=503, times=1000)
    settings.commerce_apis = {"leon": CommerceApi(base_url=BASE, token=TOKEN)}
    get_services().commerce = Commerce(settings, transport=fake.transport)
    conversation_id, events = ask(login("customer@leon.demo"), "SO20261020101 不想要了 退款")
    assert "暂未" in by_type(events, "final")[0]["text"]

    admin = login("admin@leon.demo")
    [row] = [r for r in _list(client, admin) if r["order_id"] == "SO20261020101"]
    assert (row["status"], row["conversation_id"], row["customer"]["name"]) == ("pending", conversation_id, "Emma")
    assert row["attempts"] >= 1 and row["error"], "how often it was sent and what came back"
    with new_session() as db:  # the reconciliation job opened a ticket for it (here: by hand)
        refund = db.get(Refund, row["id"])
        ticket = Ticket(tenant_id=refund.tenant_id, customer_id=refund.customer_id, kind="issue", priority="urgent",
                        title=f"退款 #{refund.id} 需要人工核实", conversation_id=conversation_id)
        db.add(ticket)
        db.flush()
        refund.ticket_id = ticket.id
        db.commit()
        ticket_id = ticket.id
    [row] = [r for r in _list(client, admin) if r["id"] == row["id"]]
    assert row["ticket_id"] == ticket_id

    r = client.post(f"/api/refunds/{row['id']}/settle", headers=admin,
                    json={"outcome": "succeeded", "external_id": "RF-MANUAL-9", "note": "支付后台查到已原路退回"})
    assert r.status_code == 200, r.text
    assert row["id"] not in {r["id"] for r in _list(client, admin)}, "settled: off the console's list"
    [done] = [r for r in _list(client, admin, "?status=succeeded") if r["id"] == row["id"]]
    assert (done["external_id"], done["follow_up"], done["updated_at"] is not None) == ("RF-MANUAL-9", None, True)


# ---------------------------------------------------------------- found in the review
def test_refunds_waiting_for_a_person_are_never_pushed_off_the_list(client, login):
    # P1. Newest first with a cap of 200 hid exactly the refunds that need a person: they are the
    # oldest (past the 24-hour re-send window), so 200 fresh ones pushed them off, and the navigation
    # count read 0. The default view lists them first; the count comes from its own query.
    stuck = {_add("customer@leon.demo", f"SO2026101900{i}", "pending", hours_ago=30 + i) for i in range(3)}
    with new_session() as db:
        emma = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
        start = datetime.now(timezone.utc) - timedelta(hours=2)
        db.add_all(Refund(tenant_id=emma.tenant_id, order_id=f"SO2026102{i:05d}", customer_id=emma.id, amount=1.0,
                          status="pending", idempotency_key=uuid.uuid4().hex, created_at=start + timedelta(seconds=i))
                   for i in range(200))
        db.commit()
    agent = login("agent@leon.demo")
    rows = _list(client, agent)
    assert len(rows) == 200 and {r["id"] for r in rows[:3]} == stuck, "the ones a person must settle come first"
    assert {r["follow_up"] for r in rows[:3]} == {"person"} and {r["follow_up"] for r in rows[3:]} == {"resend"}
    fresh = [r["created_at"] for r in rows[3:]]
    assert fresh == sorted(fresh, reverse=True), "then newest first"
    r = client.get("/api/refunds/summary", headers=agent)
    assert r.status_code == 200 and r.json() == {"unsettled": 203, "needs_person": 3}, r.text
    assert client.get("/api/refunds/summary", headers=login("customer@leon.demo")).status_code == 403
    assert client.get("/api/refunds/summary", headers=login("admin@iris.demo")).json() == \
        {"unsettled": 0, "needs_person": 0}, "per tenant"


def test_the_summary_counts_what_the_list_marks(client, login, leon_over_http):
    _add("customer@leon.demo", "SO20261020261", "pending", hours_ago=1)
    _add("customer@leon.demo", "SO20261020262", "pending", hours_ago=25)
    _add("customer@leon.demo", "SO20261020263", "processing", hours_ago=1, external_id="RF000263")
    _add("customer@leon.demo", "SO20261020264", "processing", hours_ago=1)  # no payment-system id: nobody follows it
    _add("customer@leon.demo", "SO20261020265", "succeeded", hours_ago=1)
    admin = login("admin@leon.demo")
    rows = _list(client, admin)
    summary = client.get("/api/refunds/summary", headers=admin).json()
    assert summary == {"unsettled": len(rows), "needs_person": sum(r["follow_up"] == "person" for r in rows)}
    assert summary["needs_person"] == 5, "two of mine and Jack's three seeded ones (no payment-system id)"


def test_a_refund_stuck_while_being_claimed_is_shown_as_a_fault(client, login):
    # P2. A committed "claiming" row is a bug that holds its order (the reconciliation job logs it);
    # the console showed it nowhere by default and with no next step.
    claiming = _add("customer@leon.demo", "SO20261020271", "claiming", hours_ago=1)
    agent = login("agent@leon.demo")
    [row] = [r for r in _list(client, agent) if r["id"] == claiming]
    assert row["follow_up"] == "stuck"
    assert client.get("/api/refunds/summary", headers=agent).json()["needs_person"] == 1
    r = client.post(f"/api/refunds/{claiming}/settle", headers=login("admin@leon.demo"),
                    json={"outcome": "succeeded", "note": "支付后台查到已原路退回"})
    assert r.status_code == 409 and "登记未完成" in r.json()["detail"], "not 'already settled': it never was"


@pytest.mark.parametrize("status, hours_ago, external_id", [
    ("pending", 2, None),  # still sent again under its key for 22 more hours
    ("processing", 2, "RF000281"),  # accepted; the job asks the payment system how it ended
])
def test_a_refund_the_job_still_follows_cannot_be_settled_as_failed(client, login, leon_over_http,
                                                                    status, hours_ago, external_id):
    # P1 (money). Recording "failed" frees the order for a second refund, while a re-send under the
    # same key, or the payment system finishing the one it accepted, can still pay the first: two
    # refunds. Until the job hands it over, a person may only record that it was paid.
    refund_id = _add("customer@leon.demo", "SO20261020281", status, hours_ago=hours_ago, external_id=external_id)
    admin = login("admin@leon.demo")
    url = f"/api/refunds/{refund_id}/settle"
    r = client.post(url, headers=admin, json={"outcome": "failed", "note": "支付后台暂时查不到这笔退款"})
    assert r.status_code == 409 and "两次" in r.json()["detail"], r.text
    with new_session() as db:
        assert db.get(Refund, refund_id).status == status, "nothing changed"
    r = client.post(url, headers=admin, json={"outcome": "succeeded", "note": "支付后台查到已原路退回"})
    assert r.status_code == 200, "a payout seen in the back office can be recorded at any time"


def test_a_refund_in_the_demo_tables_cannot_be_settled_as_failed(client, login):
    with new_session() as db:
        jacks = db.execute(select(Refund.id).where(Refund.status == "processing")).scalars().first()
    r = client.post(f"/api/refunds/{jacks}/settle", headers=login("admin@leon.demo"),
                    json={"outcome": "failed", "note": "演示数据，不经过支付系统"})
    assert r.status_code == 409, "it would leave its demo order held for good"


def test_what_the_list_says_the_job_does_is_what_the_job_does(settings, client, login, monkeypatch):
    # The console's next step is the reconciliation job's rules restated; run the job and compare.
    from app.ops.refund_reconcile import reconcile_refunds

    monkeypatch.setattr(commerce_http, "_sleep", lambda seconds: None)
    fake = FakeCommerce()
    for order_id in ("SO20261020291", "SO20261020292"):
        fake.add_order(order_id, "customer@leon.demo", 59.0)
    settings.commerce_apis = {"leon": CommerceApi(base_url=BASE, token=TOKEN)}
    commerce = Commerce(settings, transport=fake.transport)
    get_services().commerce = commerce
    rows = {
        "resend": _add("customer@leon.demo", "SO20261020291", "pending", hours_ago=3, idempotency_key="k-resend"),
        "person": _add("customer@leon.demo", "SO20261020292", "pending", hours_ago=25, idempotency_key="k-person"),
        "follow": _add("customer@leon.demo", "SO20261020293", "processing", hours_ago=3, external_id="RF000293"),
        "untracked": _add("customer@leon.demo", "SO20261020294", "processing", hours_ago=3),
    }
    listed = {r["id"]: r["follow_up"] for r in _list(client, login("agent@leon.demo"))}
    assert [listed[rows[k]] for k in rows] == ["resend", "person", "follow", "person"]

    reconcile_refunds(settings, commerce=commerce)
    sent = {p.headers["idempotency-key"] for p in fake.calls("POST", "/loopdesk/refunds")}
    queried = {p.url.path.rsplit("/", 1)[-1] for p in fake.calls("GET", "/loopdesk/refunds/")}
    assert sent == {"k-resend"}, "re-sent: only the one the list says is re-sent"
    assert queried == {"RF000293"}, "followed: only the one the list says is followed"
    with new_session() as db:
        ticket = db.get(Ticket, db.get(Refund, rows["person"]).ticket_id)
        assert "k-person" in ticket.description, "the ticket names the key the back office searches by"
