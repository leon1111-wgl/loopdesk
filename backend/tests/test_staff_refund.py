"""A member of staff filing a refund for a customer (N-146). An agent's chat used to pay a customer's
order out automatically when it was under the tenant's limit: no approval, no audit entry, and the
refund row named only the customer. Now every staff-filed refund waits for another person."""

import pytest
from sqlalchemy import func, select

from app.agent.business_tools import STAFF_FILED_REASON, _refund_policy, request_refund
from app.agent.runtime import RunOptions, collect
from app.agent.tools import ApprovalRequired, ToolContext
from app.db import new_session
from app.models import Approval, AuditLog, Order, Refund, Tenant, User
from app.services import get_services
from tests.conftest import by_type

ECHO_ORDER = "SO20261019101"  # Lily's ¥18 renewal, charged yesterday: automatic for Lily herself
LEON_ORDER = "SO20260918001"  # Emma's ¥39.90, under Leon's ¥50 limit


def _user_id(email: str) -> int:
    with new_session() as db:
        return db.execute(select(User.id).where(User.email == email)).scalar_one()


@pytest.mark.parametrize("staff, admin, order, text", [
    ("agent@echo.demo", "admin@echo.demo", ECHO_ORDER, f"{ECHO_ORDER} 刚被扣了续费，帮我退款"),  # the report
    ("agent@leon.demo", "admin@leon.demo", LEON_ORDER, f"订单 {LEON_ORDER} 申请退款"),
    ("admin@leon.demo", "agent@leon.demo", LEON_ORDER, f"订单 {LEON_ORDER} 申请退款"),  # administrators too
])
def test_a_refund_filed_by_staff_waits_for_another_person(client, ask, login, staff, admin, order, text):
    requester = login(staff)
    _, events = ask(requester, text)
    pending = by_type(events, "approval_required")
    assert pending and STAFF_FILED_REASON in pending[0]["reason"]
    approval_id = pending[0]["approval_id"]
    with new_session() as db:
        assert db.execute(select(func.count(Refund.id)).where(Refund.order_id == order)).scalar_one() == 0
        assert db.get(Order, order).status != "refunding", "no money moves before approval"
        approval = db.get(Approval, approval_id)
        assert approval.requested_by == _user_id(staff) and STAFF_FILED_REASON in approval.reason
        entry = db.execute(select(AuditLog).where(AuditLog.action == "approval.request")).scalar_one()
        assert entry.actor_id == _user_id(staff) and entry.target == f"approval:{approval_id}"
        assert entry.detail["order_id"] == order and entry.detail["tool"] == "request_refund"
        assert entry.detail["amount"] == approval.arguments["amount"]
    final = by_type(events, "final")[0]["text"]
    assert final.startswith(f"已为顾客提交订单 {order} 的退款申请") and "您的退款申请" not in final

    # The requester cannot approve it; someone else can, and the refund points at the approval.
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=requester)
    assert r.status_code == 403
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=login(admin))
    assert r.status_code == 200, r.text
    with new_session() as db:
        refund = db.execute(select(Refund).where(Refund.order_id == order)).scalar_one()
        assert refund.approval_id == approval_id
        approved = db.execute(select(AuditLog).where(AuditLog.action == "approval.approve")).scalar_one()
        assert approved.actor_id == _user_id(admin)
        # the resume does not file the application again
        assert db.execute(select(func.count(AuditLog.id)).where(AuditLog.action == "approval.request")).scalar_one() == 1


def test_a_staff_rehearsal_files_nothing(client):
    # Evaluations and the MCP server run in rehearsal mode: no application, no audit entry, no money.
    with new_session() as db:
        chloe = db.execute(select(User).where(User.email == "agent@echo.demo")).scalar_one()
        summary = collect(get_services().runtime(db).start(
            tenant=db.get(Tenant, chloe.tenant_id), user=chloe, text=f"{ECHO_ORDER} 刚被扣了续费，帮我退款",
            options=RunOptions(dry_run=True)))
        assert summary["approval"] and summary["approval"]["approval_id"] is None
    with new_session() as db:
        assert db.execute(select(func.count(Approval.id))).scalar_one() == 0
        assert db.execute(select(func.count(AuditLog.id)).where(AuditLog.action == "approval.request")).scalar_one() == 0
        assert db.execute(select(func.count(Refund.id)).where(Refund.order_id == ECHO_ORDER)).scalar_one() == 0


def test_the_customer_still_gets_the_automatic_refund(ask, login):
    _, events = ask(login("customer@echo.demo"), f"{ECHO_ORDER} 刚被扣了续费，帮我退款")
    assert not by_type(events, "approval_required")
    with new_session() as db:
        assert db.execute(select(Refund).where(Refund.order_id == ECHO_ORDER)).scalar_one().approval_id is None
        assert db.execute(select(func.count(AuditLog.id)).where(AuditLog.action == "approval.request")).scalar_one() == 0


def test_policy_and_handler_each_refuse_to_pay_a_staff_request_without_an_approval(settings, client):
    # The policy is the gate; the handler re-checks under its locks, as it does for the velocity limit.
    # Either one alone pauses the run, so the end-to-end tests above cannot tell them apart.
    with new_session() as db:
        chloe = db.execute(select(User).where(User.email == "agent@echo.demo")).scalar_one()
        ctx = ToolContext(db=db, settings=settings, tenant=db.get(Tenant, chloe.tenant_id), user=chloe,
                          embedder=None, run_id="test", conversation_id=None)
        assert STAFF_FILED_REASON in _refund_policy(ctx, {"order_id": ECHO_ORDER, "amount": 18.0}).reason
        with pytest.raises(ApprovalRequired):
            request_refund(ctx, {"order_id": ECHO_ORDER, "amount": 18.0, "reason": "续费"})
        assert db.execute(select(func.count(Refund.id)).where(Refund.order_id == ECHO_ORDER)).scalar_one() == 0
