"""A rejected refund filed again (N-069). The second application was a fresh card with no trace of the
first: another reviewer could approve what a colleague had just rejected, and under the automatic limit
the customer's own request was paid without anyone looking. Now a rejection of the same order in the
last 30 days sends the new application to a person, and every card lists the order's earlier ones."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db import new_session
from app.models import Approval, Refund, Tenant
from tests.conftest import by_type

SMALL = "SO20260918001"  # Emma's ¥39.90, under Leon's ¥50 automatic limit
LARGE = "SO20261008002"  # Emma's ¥1299, always reviewed


def _refunds(order: str) -> int:
    with new_session() as db:
        return db.execute(select(func.count(Refund.id)).where(Refund.order_id == order)).scalar_one()


def _reject(client, approval_id: int, headers: dict, comment: str) -> None:
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "reject", "comment": comment},
                    headers=headers)
    assert r.status_code == 200, r.text


def _staff_filed_then_rejected(client, ask, login, comment: str = "照片看不出质量问题") -> int:
    _, events = ask(login("agent@leon.demo"), f"订单 {SMALL} 申请退款")
    first = by_type(events, "approval_required")[0]["approval_id"]
    _reject(client, first, login("admin@leon.demo"), comment)
    return first


def test_a_rejected_small_refund_asked_again_waits_for_a_person(client, ask, login):
    # The bypass: Mia files for Emma, Leon rejects it, Emma asks herself under the limit. It used to be paid.
    first = _staff_filed_then_rejected(client, ask, login)
    _, events = ask(login("customer@leon.demo"), f"订单 {SMALL} 申请退款")
    pending = by_type(events, "approval_required")
    assert pending, "a rejected order is not paid automatically when asked again"
    assert _refunds(SMALL) == 0
    final = by_type(events, "final")[0]["text"]
    assert "驳回过" not in final and "驳回过" not in pending[0]["reason"], \
        "the risk control is for the reviewer, not read to the customer"

    card = client.get(f"/api/approvals/{pending[0]['approval_id']}", headers=login("admin@leon.demo")).json()
    assert "驳回过" in card["reason"] and f"#{first}" in card["reason"]
    assert "照片看不出质量问题" in card["reason"] and "Leon" in card["reason"]
    [earlier] = card["history"]
    assert {k: earlier[k] for k in ("id", "status", "amount", "reviewer", "comment")} == {
        "id": first, "status": "rejected", "amount": 39.9, "reviewer": "Leon", "comment": "照片看不出质量问题"}
    assert earlier["created_at"] and earlier["decided_at"] and earlier["decided_at"] >= earlier["created_at"]


def test_the_queue_shows_each_card_its_own_order_history(client, ask, login):
    first = _staff_filed_then_rejected(client, ask, login)
    emma, mia = login("customer@leon.demo"), login("agent@leon.demo")
    _, events = ask(emma, f"{LARGE} 我要退款")
    large = by_type(events, "approval_required")[0]["approval_id"]
    _reject(client, large, mia, "屏幕碎裂属于人为损坏")
    _, events = ask(emma, f"{LARGE} 我要退款")
    again = by_type(events, "approval_required")[0]["approval_id"]
    _, events = ask(emma, f"订单 {SMALL} 申请退款")
    small = by_type(events, "approval_required")[0]["approval_id"]

    cards = {c["id"]: c for c in client.get("/api/approvals?status_filter=all", headers=login("admin@leon.demo")).json()}
    assert [h["id"] for h in cards[again]["history"]] == [large]
    assert cards[again]["history"][0]["reviewer"] == "Mia" and cards[again]["history"][0]["comment"] == "屏幕碎裂属于人为损坏"
    assert [h["id"] for h in cards[small]["history"]] == [first]
    assert cards[first]["history"] == [] and cards[large]["history"] == [], "only earlier applications"
    # the page reads the same history as a single card
    single = client.get(f"/api/approvals/{again}", headers=login("admin@leon.demo")).json()
    assert single["history"] == cards[again]["history"]


def test_a_rejection_older_than_30_days_is_history_only(client, ask, login):
    first = _staff_filed_then_rejected(client, ask, login)
    with new_session() as db:
        db.get(Approval, first).decided_at = datetime.now(timezone.utc) - timedelta(days=31)
        db.commit()
    _, events = ask(login("customer@leon.demo"), f"订单 {SMALL} 申请退款")
    assert not by_type(events, "approval_required")
    assert _refunds(SMALL) == 1


def test_a_withdrawn_application_does_not_force_review(client, ask, login):
    # Only a rejection is someone's decision; Mia taking her own application back is not.
    mia = login("agent@leon.demo")
    _, events = ask(mia, f"订单 {SMALL} 申请退款")
    first = by_type(events, "approval_required")[0]["approval_id"]
    assert client.post(f"/api/approvals/{first}/withdraw", headers=mia).status_code == 200
    _, events = ask(login("customer@leon.demo"), f"订单 {SMALL} 申请退款")
    assert not by_type(events, "approval_required")
    assert _refunds(SMALL) == 1


def test_staff_filing_again_also_goes_to_review_with_the_rejection(client, ask, login):
    first = _staff_filed_then_rejected(client, ask, login, comment="凭证不足" * 40)
    _, events = ask(login("agent@leon.demo"), f"订单 {SMALL} 申请退款")
    again = by_type(events, "approval_required")[0]["approval_id"]
    card = client.get(f"/api/approvals/{again}", headers=login("admin@leon.demo")).json()
    assert f"#{first}" in card["reason"] and "…" in card["reason"], "a long comment is clipped in the reason"
    assert card["history"][0]["comment"] == "凭证不足" * 40, "and kept whole on the card"


def test_a_rejection_in_another_tenant_does_not_count(client, ask, login):
    first = _staff_filed_then_rejected(client, ask, login)
    with new_session() as db:
        iris = db.execute(select(Tenant).where(Tenant.slug == "iris")).scalar_one()
        db.get(Approval, first).tenant_id = iris.id
        db.commit()
    _, events = ask(login("customer@leon.demo"), f"订单 {SMALL} 申请退款")
    assert not by_type(events, "approval_required")
    assert _refunds(SMALL) == 1


def test_a_much_reapplied_order_keeps_five_on_each_card(client, ask, login):
    emma, mia = login("customer@leon.demo"), login("agent@leon.demo")
    ids = []
    for n in range(7):
        _, events = ask(emma, f"{LARGE} 我要退款")
        ids.append(by_type(events, "approval_required")[0]["approval_id"])
        if n < 6:
            _reject(client, ids[-1], mia, f"第 {n + 1} 次驳回")
    cards = {c["id"]: c for c in client.get("/api/approvals?status_filter=all", headers=login("admin@leon.demo")).json()}
    assert [h["id"] for h in cards[ids[6]]["history"]] == ids[5:0:-1]
    assert [h["id"] for h in cards[ids[3]]["history"]] == ids[2::-1]
    assert cards[ids[3]]["history"] == client.get(f"/api/approvals/{ids[3]}", headers=login("admin@leon.demo")).json()["history"]
