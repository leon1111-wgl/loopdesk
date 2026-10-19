"""Two refund requests for the same order at the same time must not both pay out."""

import threading

from sqlalchemy import select

from app.agent import business_tools
from app.agent.tools import ToolContext, ToolError
from app.db import new_session
from app.models import Order, Refund, Tenant, User


def test_concurrent_refunds_for_one_order_pay_out_once(settings, client, monkeypatch):
    # Force the worst interleaving: both requests load the order (status "delivered")
    # before either of them writes.
    barrier = threading.Barrier(2, timeout=5)
    real_load = business_tools._load_order

    def load_then_wait(ctx, order_id):
        order = real_load(ctx, order_id)
        if threading.current_thread().name.startswith("refund-"):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return order

    monkeypatch.setattr(business_tools, "_load_order", load_then_wait)
    results = []

    def attempt():
        with new_session() as db:
            emma = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
            ctx = ToolContext(db=db, settings=settings, tenant=db.get(Tenant, emma.tenant_id), user=emma,
                              embedder=None, run_id="race", conversation_id=None)
            try:
                results.append(business_tools.request_refund(ctx, {"order_id": "SO20260918001", "reason": "不想要了"}))
            except ToolError as exc:
                results.append({"error": str(exc)})

    threads = [threading.Thread(target=attempt, name=f"refund-{i}") for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    with new_session() as db:
        refunds = db.execute(select(Refund).where(Refund.order_id == "SO20260918001")).scalars().all()
        assert len(refunds) == 1, f"paid out {len(refunds)} times: {results}"
        assert db.get(Order, "SO20260918001").status == "refunding"
    assert sum(1 for r in results if "error" in r) == 1
