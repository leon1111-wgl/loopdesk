"""A staff-filed refund nobody can approve (N-162). Leon is the only administrator of his tenant: a
¥899 refund he filed for Jack needs an administrator (over ¥500), the four-eyes rule keeps him off it,
and Mia may only reject. It sat in the queue for good, blocking Jack's own application, and Leon could
not take it back either. Now the application is not filed when nobody else could approve it, and the
person who filed one can withdraw it (withdrawing decides nothing, so the four-eyes rule is not in play)."""

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db import new_session
from app.models import Approval, AuditLog, Message, Order, Refund, Run, User
from app.services import get_services
from tests.conftest import by_type

BIG = "SO20261003005"  # Jack's ¥899 dishwasher: above Leon's ¥500 administrator line
SMALL = "SO20260918001"  # Emma's ¥39.90: any other member of staff may approve


def _user(email: str) -> User:
    with new_session() as db:
        return db.execute(select(User).where(User.email == email)).scalar_one()


def _approvals() -> int:
    with new_session() as db:
        return db.execute(select(func.count(Approval.id))).scalar_one()


def test_the_only_administrator_is_told_instead_of_filing_a_dead_application(ask, login):
    _, events = ask(login("admin@leon.demo"), f"{BIG} 顾客要求全额退款 899 元")
    assert not by_type(events, "approval_required")
    final = by_type(events, "final")[0]["text"]
    assert "没有其他管理员" in final and "顾客本人" in final, final
    assert _approvals() == 0
    with new_session() as db:
        assert db.get(Order, BIG).status == "delivered"
        assert db.execute(select(func.count(Refund.id)).where(Refund.order_id == BIG)).scalar_one() == 0


def test_jack_can_still_apply_himself_after_that(ask, login):
    ask(login("admin@leon.demo"), f"{BIG} 顾客要求全额退款 899 元")
    _, events = ask(login("customer2@leon.demo"), f"{BIG} 洗碗机漏水 我要退款")
    assert by_type(events, "approval_required"), "nothing of Leon's stands in the way"


def test_an_agent_filing_the_same_refund_still_gets_it_to_the_administrator(ask, login):
    _, events = ask(login("agent@leon.demo"), f"{BIG} 顾客要求全额退款 899 元")
    assert by_type(events, "approval_required")  # Leon may approve what Mia filed


def test_below_the_line_the_agent_is_enough(ask, login):
    _, events = ask(login("admin@leon.demo"), f"订单 {SMALL} 申请退款")
    assert by_type(events, "approval_required")  # Mia may approve ¥39.90


def test_a_disabled_colleague_does_not_count(ask, login):
    with new_session() as db:
        mia = db.execute(select(User).where(User.email == "agent@leon.demo")).scalar_one()
        mia.disabled_at = datetime.now(timezone.utc)
        db.commit()
    _, events = ask(login("admin@leon.demo"), f"订单 {SMALL} 申请退款")
    assert not by_type(events, "approval_required")
    assert "没有其他" in by_type(events, "final")[0]["text"]
    assert _approvals() == 0


def _file_as_mia(ask, login) -> tuple[dict, int, int]:
    mia = login("agent@leon.demo")
    conversation_id, events = ask(mia, f"{BIG} 顾客要求全额退款 899 元")
    return mia, conversation_id, by_type(events, "approval_required")[0]["approval_id"]


def test_the_requester_withdraws_their_own_application(client, ask, login):
    mia, conversation_id, approval_id = _file_as_mia(ask, login)
    r = client.post(f"/api/approvals/{approval_id}/withdraw", headers=mia)
    assert r.status_code == 200, r.text
    assert r.json()["approval"]["status"] == "withdrawn"
    with new_session() as db:
        approval = db.get(Approval, approval_id)
        assert approval.status == "withdrawn" and approval.reviewer_id is None and approval.decided_at
        assert db.get(Run, approval.run_id).status == "completed"
        assert not (db.get(Run, approval.run_id).state or {}).get("pending")
        entry = db.execute(select(AuditLog).where(AuditLog.action == "approval.withdraw")).scalar_one()
        assert entry.actor_id == _user("agent@leon.demo").id and entry.target == f"approval:{approval_id}"
        assert entry.detail["amount"] == 899.0 and entry.detail["order_id"] == BIG
        last = db.execute(select(Message).where(Message.conversation_id == conversation_id)
                          .order_by(Message.id.desc()).limit(1)).scalar_one()
        assert "撤回" in last.content and BIG in last.content  # the chat that said "waiting" is closed too
    # A reviewer who opens it afterwards cannot pay it out, and is not told the customer withdrew.
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"},
                    headers=login("admin@leon.demo"))
    assert r.status_code == 409 and "提交人已撤回" in r.json()["detail"]
    with new_session() as db:
        assert db.execute(select(func.count(Refund.id)).where(Refund.order_id == BIG)).scalar_one() == 0
    listed = client.get("/api/approvals?status_filter=all", headers=mia).json()
    assert listed[0]["withdrawn_by"] == "requester"


def test_only_the_requester_may_withdraw_and_only_while_pending(client, ask, login):
    _, _, approval_id = _file_as_mia(ask, login)
    admin = login("admin@leon.demo")
    r = client.post(f"/api/approvals/{approval_id}/withdraw", headers=admin)
    assert r.status_code == 403
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "reject", "comment": "金额不对"},
                    headers=admin)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/approvals/{approval_id}/withdraw", headers=login("agent@leon.demo"))
    assert r.status_code == 409
    with new_session() as db:
        assert db.get(Approval, approval_id).status == "rejected"


def test_a_customer_cannot_use_the_staff_withdrawal(client, ask, login):
    customer = login("customer2@leon.demo")
    _, events = ask(customer, f"{BIG} 洗碗机漏水 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    r = client.post(f"/api/approvals/{approval_id}/withdraw", headers=customer)
    assert r.status_code == 403  # customers withdraw in the chat, with a confirmation (N-145)
    with new_session() as db:
        assert db.get(Approval, approval_id).status == "pending"


def test_withdrawing_after_a_failed_decision_closes_the_chat_too(client, ask, login, monkeypatch):
    # A decision whose resume broke is reopened: the approval is pending again, its run "failed" but still
    # paused. Withdrawing it must end that run, or the requester's chat keeps polling "waiting" (review).
    mia, conversation_id, approval_id = _file_as_mia(ask, login)

    def broken(*args, **kwargs):
        raise RuntimeError("connection reset by upstream")

    monkeypatch.setattr(get_services().provider, "stream", broken)
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "reject", "comment": "金额不对"},
                    headers=login("admin@leon.demo"))
    assert r.status_code == 503, r.text
    monkeypatch.undo()
    assert client.post(f"/api/approvals/{approval_id}/withdraw", headers=mia).status_code == 200
    with new_session() as db:
        run = db.get(Run, db.get(Approval, approval_id).run_id)
        assert run.status == "completed" and not (run.state or {}).get("pending")
        last = db.execute(select(Message).where(Message.conversation_id == conversation_id)
                          .order_by(Message.id.desc()).limit(1)).scalar_one()
        assert "撤回" in last.content and (last.meta or {}).get("status") == "completed"
