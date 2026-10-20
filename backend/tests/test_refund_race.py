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


def test_concurrent_small_refunds_cannot_get_past_the_velocity_limit(settings, client, monkeypatch):
    # Review round 5 (red team), N-039: the limit of 3 automatic refunds per customer in 30 days
    # was checked with a plain read before the refund ran, and executing it locked only the order.
    # Five requests for five different orders at once all read "no refunds yet" and all paid out.
    from datetime import datetime, timedelta, timezone

    from app.agent.tracing import Tracer
    from app.llm.base import ToolCall
    from app.models import Run
    from app.services import get_services

    orders = [f"SO2026101990{i}" for i in range(5)]
    with new_session() as db:
        emma = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
        for order_id in orders:
            db.add(Order(id=order_id, tenant_id=emma.tenant_id, customer_id=emma.id, amount=30.0, status="paid",
                         items=[{"name": "测试数据线", "qty": 1, "price": 30.0}],
                         paid_at=datetime.now(timezone.utc) - timedelta(days=1)))
        db.commit()

    # Worst interleaving: every request reads the refund history before any of them pays out.
    barrier = threading.Barrier(len(orders), timeout=5)
    real_history = business_tools._refund_history
    waited: set[str] = set()

    def read_then_wait(ctx, customer_id):
        result = real_history(ctx, customer_id)
        name = threading.current_thread().name
        if name.startswith("refund-") and name not in waited:  # only the policy's read waits
            waited.add(name)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return result

    monkeypatch.setattr(business_tools, "_refund_history", read_then_wait)
    outcomes: dict[str, str] = {}

    def attempt(order_id: str):
        with new_session() as db:
            emma = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
            run = Run(tenant_id=emma.tenant_id, user_id=emma.id, input_text="race", status="running")
            db.add(run)
            db.commit()
            ctx = ToolContext(db=db, settings=settings, tenant=db.get(Tenant, emma.tenant_id), user=emma,
                              embedder=None, run_id=run.id, conversation_id=None)
            outcome = get_services().runtime(db)._run_tool(
                Tracer(db, run, settings), ctx,
                ToolCall(id=order_id, name="request_refund", arguments={"order_id": order_id, "reason": "不想要了"}))
            outcomes[order_id] = outcome.kind if outcome.kind == "approval" else outcome.output.get("status", "error")

    threads = [threading.Thread(target=attempt, args=(o,), name=f"refund-{i}") for i, o in enumerate(orders)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with new_session() as db:
        paid = db.execute(select(Refund).where(Refund.order_id.in_(orders))).scalars().all()
        statuses = {o.id: o.status for o in db.execute(select(Order).where(Order.id.in_(orders))).scalars()}
    assert len(paid) == settings.refund_window_max_count, outcomes
    assert sorted(outcomes.values()) == ["approval"] * 2 + ["auto_approved"] * 3, outcomes
    assert sorted(statuses.values()) == ["paid"] * 2 + ["refunding"] * 3, "an escalated order is not left claimed"


def test_on_postgresql_the_customer_lock_queues_refunds_but_not_the_customers_chats(settings, client, monkeypatch):
    # N-039 on PostgreSQL, where the re-check is only safe because two automatic refunds for one
    # customer cannot run it at the same time. While one holds the customer's lock, a second one for
    # another order waits for it; a chat of the same customer (a run row, whose foreign key takes
    # FOR KEY SHARE on the customer) goes straight through, which a plain FOR UPDATE would block.
    from datetime import datetime, timedelta, timezone

    import pytest
    from sqlalchemy import text

    from app.models import Run

    if not settings.database_url.startswith("postgresql"):
        pytest.skip("SQLite has one writer by design; the row-lock modes are PostgreSQL's")
    first, second = "SO20261019910", "SO20261019911"
    with new_session() as db:
        emma = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
        emma_id, tenant_id = emma.id, emma.tenant_id
        for order_id in (first, second):
            db.add(Order(id=order_id, tenant_id=tenant_id, customer_id=emma_id, amount=30.0, status="paid",
                         items=[{"name": "测试数据线", "qty": 1, "price": 30.0}],
                         paid_at=datetime.now(timezone.utc) - timedelta(days=1)))
        db.commit()

    inside, release = threading.Event(), threading.Event()
    real_velocity = business_tools._velocity_reason

    def hold_the_lock(ctx, customer_id, amount):  # the re-check runs under the customer's lock
        if threading.current_thread().name == "refund-first":
            inside.set()
            release.wait(10)
        return real_velocity(ctx, customer_id, amount)

    monkeypatch.setattr(business_tools, "_velocity_reason", hold_the_lock)
    results: dict[str, dict] = {}

    def refund(order_id: str, lock_timeout: str | None):
        with new_session() as db:
            if lock_timeout:
                db.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
            ctx = ToolContext(db=db, settings=settings, tenant=db.get(Tenant, tenant_id), user=db.get(User, emma_id),
                              embedder=None, run_id="lock", conversation_id=None)
            try:
                results[order_id] = business_tools.request_refund(ctx, {"order_id": order_id, "reason": "不想要了"})
            except Exception as exc:  # a lock timeout surfaces as an OperationalError
                results[order_id] = {"error": str(exc)}

    holder = threading.Thread(target=refund, args=(first, None), name="refund-first")
    holder.start()
    try:
        assert inside.wait(10)
        with new_session() as db:  # the same customer starts a chat meanwhile
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            db.add(Run(tenant_id=tenant_id, user_id=emma_id, input_text="退款到哪一步了", status="running"))
            db.commit()
        waiter = threading.Thread(target=refund, args=(second, "1s"), name="refund-second")
        waiter.start()
        waiter.join(15)
    finally:
        release.set()
        holder.join(15)
    assert results[first]["status"] == "auto_approved", results
    assert "lock timeout" in results[second].get("error", ""), results


def test_a_refund_escalated_under_the_lock_keeps_what_the_run_has_not_saved(settings, client, login, ask, monkeypatch):
    # Review of N-039: undoing the claim rolled the whole session back. With buffered trace writes
    # (the PostgreSQL default) the run's model and route were not committed yet, so the run row
    # lost them; the per-span mode had committed them with an earlier span.
    from app.models import Run

    from tests.conftest import by_type

    settings.trace_writes = "buffered"
    real = business_tools._refund_history
    calls: list[int] = []

    def history(ctx, customer_id):
        calls.append(1)
        return real(ctx, customer_id) if len(calls) == 1 else (3, 90.0)  # three refunds landed meanwhile

    monkeypatch.setattr(business_tools, "_refund_history", history)
    _, events = ask(login("customer@leon.demo"), "SO20260918001 不想要了 退款")
    assert by_type(events, "approval_required"), [e["type"] for e in events]
    with new_session() as db:
        run = db.get(Run, by_type(events, "done")[0]["run_id"])
        assert run.status == "waiting_approval"
        assert run.model and run.route_tier, (run.model, run.route_tier)
        assert db.get(Order, "SO20260918001").status == "delivered", "the escalated order is not left claimed"
