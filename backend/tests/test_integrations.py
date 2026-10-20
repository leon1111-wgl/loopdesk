"""The order and payment systems behind the agent's tools (N-028).

The tools reach orders, shipments and refunds through gateways (app/integrations): the demo
tables by default, or a merchant's own systems over HTTP, configured per tenant. Here the HTTP
side is tests/fake_commerce.py, an in-process server reached through httpx.MockTransport.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, update

from app.agent.business_tools import _refund_executed, file_refund, list_orders, request_refund
from app.agent.tools import Evidence, ToolContext, ToolError
from app.agent.verifier import verify_answer
from app.config import CommerceApi, Settings
from app.db import new_session
from app.evals.runner import run_suite
from app.integrations import Commerce, GatewayError, RefundRejected, RefundRequest, commerce_deadline
from app.integrations import http as commerce_http
from app.integrations.local import LocalRefundGateway
from app.models import Approval, EvalCase, Order, Refund, Run, Span, Tenant, User
from app.observability import request_id_var
from app.ops.refund_reconcile import reconcile_refunds
from app.services import get_services
from tests.conftest import by_type
from tests.fake_commerce import TOKEN, FakeCommerce

BASE = "https://oms.leon.test/loopdesk"


@pytest.fixture()
def no_backoff(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(commerce_http, "_sleep", sleeps.append)
    return sleeps


@pytest.fixture()
def remote(settings, client, no_backoff):
    """Leon优选's orders and payments live in its own systems; Iris云文档 keeps the demo tables."""
    fake = FakeCommerce()
    fake.add_order("SO20261020101", "customer@leon.demo", 39.0,
                   items=[{"name": "远程云朵抱枕", "qty": 1, "price": 39.0}])
    fake.add_order("SO20261020102", "customer@leon.demo", 1500.0, status="shipped", delivered_days_ago=None,
                   shipment={"carrier": "Atlas快运", "tracking_no": "AT9000000102", "events": [
                       {"time": "2026-10-19 08:00", "desc": "已揽收（深圳）"},
                       {"time": "2026-10-20 09:30", "desc": "派送中，快递员正在派件"}]})
    fake.add_order("SO20261020103", "customer2@leon.demo", 45.0)
    settings.commerce_apis = {"leon": CommerceApi(base_url=BASE, token=TOKEN)}
    get_services().commerce = Commerce(settings, transport=fake.transport)
    return fake


def _final(events: list[dict]) -> str:
    return by_type(events, "final")[0]["text"]


def _ledger(order_id: str) -> list[Refund]:
    with new_session() as db:
        return db.execute(select(Refund).where(Refund.order_id == order_id).order_by(Refund.id)).scalars().all()


def _span(events: list[dict], name: str) -> Span:
    with new_session() as db:
        return db.execute(select(Span).where(Span.run_id == by_type(events, "done")[0]["run_id"], Span.name == name)
                          .order_by(Span.id.desc())).scalars().first()


def _user_id(email: str) -> int:
    with new_session() as db:
        return db.execute(select(User.id).where(User.email == email)).scalar_one()


# ---------------------------------------------------------------- the agent, over HTTP
def test_order_questions_are_answered_from_the_order_system(remote, ask, login):
    _, events = ask(login("customer@leon.demo"), "SO20261020101 这个订单什么状态")
    text = _final(events)
    assert "远程云朵抱枕" in text and "39.00" in text, text
    assert [r.url.path for r in remote.calls("GET", "/loopdesk/orders")] == ["/loopdesk/orders/SO20261020101"]
    with new_session() as db:
        assert db.get(Order, "SO20261020101") is None, "nothing is copied into the demo tables"
    [call] = _span(events, "get_order").output["integration"]
    assert (call["system"], call["via"], call["status"], call["attempts"]) == ("orders", "http", 200, 1), call


def test_a_customer_cannot_read_another_customers_order_in_the_order_system(remote, ask, login):
    _, events = ask(login("customer@leon.demo"), "SO20261020103 这个订单什么状态")
    text = _final(events)
    assert "订单不存在或无权查看" in text and "45.00" not in text and "远程测试商品" not in text, text
    assert remote.calls("GET", "/loopdesk/orders/SO20261020103"), "the order system was asked"
    _, events = ask(login("customer2@leon.demo"), "SO20261020103 这个订单什么状态")
    assert "45.00" in _final(events)


def test_tracking_comes_from_the_order_system(remote, ask, login):
    _, events = ask(login("customer@leon.demo"), "SO20261020102 的快递到哪了")
    text = _final(events)
    assert "AT9000000102" in text and "派送中" in text, text
    assert remote.calls("GET", "/loopdesk/orders/SO20261020102/shipment")


def test_a_small_refund_is_paid_once_through_the_payment_system(remote, ask, login):
    _, events = ask(login("customer@leon.demo"), "SO20261020101 不想要了 退款")
    assert "已受理" in _final(events), _final(events)
    [post] = remote.calls("POST", "/loopdesk/refunds")
    [refund] = _ledger("SO20261020101")
    assert (refund.status, refund.external_id, refund.amount) == ("processing", "RF000001", 39.0)
    assert refund.customer_id == _user_id("customer@leon.demo")
    assert post.headers["idempotency-key"] == refund.idempotency_key
    body = json.loads(post.content)
    assert body == {"order_id": "SO20261020101", "amount_fen": 3900, "reason": refund.reason, "approval_id": None,
                    "loopdesk_refund_id": refund.id}, "built from the ledger row alone, amounts in fen"
    calls = _span(events, "request_refund").output["integration"]
    assert [(c["system"], c["call"]) for c in calls][-1] == ("refunds", "create_refund")


def test_an_approved_refund_is_paid_once_under_its_ledger_key(remote, client, ask, login):
    _, events = ask(login("customer@leon.demo"), "SO20261020102 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    assert not remote.calls("POST", "/loopdesk/refunds"), "nothing is paid before a person decides"
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"},
                    headers=login("admin@leon.demo"))
    assert r.status_code == 200 and "已受理" in r.json()["final"]["text"], r.text
    services = get_services()
    with new_session() as db:  # the approved call runs again (a retry): answered from the ledger
        approval = db.get(Approval, approval_id)
        run, tenant = db.get(Run, approval.run_id), db.get(Tenant, approval.tenant_id)
        ctx = ToolContext(db=db, settings=services.settings, tenant=tenant, user=db.get(User, run.user_id),
                          embedder=services.embedder, run_id=run.id, conversation_id=run.conversation_id,
                          approval_id=approval.id, orders=services.commerce.orders(tenant, db),
                          refunds=services.commerce.refunds(tenant, db))
        tool = services.registry.get("request_refund")
        again = tool.handler(ctx, {**approval.arguments, **tool.precheck(ctx, approval.arguments)})
    [refund] = _ledger("SO20261020102")
    assert len(remote.calls("POST", "/loopdesk/refunds")) == 1 and len(remote.refunds) == 1
    assert again["refund_id"] == refund.id and refund.approval_id == approval_id


def test_a_lost_reply_is_sent_again_under_the_same_key_and_pays_once(remote, ask, login):
    remote.fail("POST", "/loopdesk/refunds", error=httpx.ReadTimeout, after=True)  # made, but the reply is lost
    _, events = ask(login("customer@leon.demo"), "SO20261020101 不想要了 退款")
    assert "已受理" in _final(events), _final(events)
    posts = remote.calls("POST", "/loopdesk/refunds")
    assert len(posts) == 2 and len({p.headers["idempotency-key"] for p in posts}) == 1
    assert len(remote.refunds) == 1
    [refund] = _ledger("SO20261020101")
    assert (refund.status, refund.external_id) == ("processing", "RF000001")


def test_a_refund_nobody_confirmed_stays_pending_keeps_the_order_and_is_settled_later(remote, settings, ask, login):
    remote.fail("POST", "/loopdesk/refunds", status=503, times=3)
    customer = login("customer@leon.demo")
    _, events = ask(customer, "SO20261020101 不想要了 退款")
    text = _final(events)
    assert "暂未" in text and "工作日" not in text and "已受理" not in text, "no payout is promised: " + text
    [refund] = _ledger("SO20261020101")
    assert refund.status == "pending" and refund.external_id is None

    _, events = ask(customer, "SO20261020101 不想要了 退款")
    assert "已有一笔退款正在处理" in _final(events), _final(events)
    assert len(remote.calls("POST", "/loopdesk/refunds")) == 3, "the second request never reached the payment system"

    counts = reconcile_refunds(settings, commerce=get_services().commerce, older_than=timedelta(0))
    assert counts["confirmed"] == 1, counts
    [refund] = _ledger("SO20261020101")
    assert (refund.status, refund.external_id) == ("processing", "RF000001") and len(remote.refunds) == 1
    assert remote.calls("POST", "/loopdesk/refunds")[-1].headers["idempotency-key"] == refund.idempotency_key


def test_reconciliation_follows_a_processing_refund_to_its_end(remote, settings, ask, login):
    _, events = ask(login("customer@leon.demo"), "SO20261020101 不想要了 退款")
    assert "已受理" in _final(events)
    remote.refunds["RF000001"]["status"] = "succeeded"
    counts = reconcile_refunds(settings, commerce=get_services().commerce, older_than=timedelta(0))
    assert counts["succeeded"] == 1, counts
    assert _ledger("SO20261020101")[0].status == "succeeded"
    assert remote.calls("GET", "/loopdesk/refunds/RF000001")


def test_a_refund_the_payment_system_refuses_is_released_and_explained(remote, ask, login):
    remote.refuse("SO20261020101", 409, "refund_declined", "风控规则 R-17 命中，商户号 1234567890")
    customer = login("customer@leon.demo")
    _, events = ask(customer, "SO20261020101 不想要了 退款")
    text = _final(events)
    assert "支付系统没有受理" in text and "R-17" not in text and "1234567890" not in text, text
    [refund] = _ledger("SO20261020101")
    assert (refund.status, refund.error) == ("failed", "refund_declined")
    span = _span(events, "request_refund")
    assert span.status == "rejected" and span.output["integration"][-1]["code"] == "refund_declined", span.output

    del remote.refusals["SO20261020101"]  # the claim was released: a new request goes through
    _, events = ask(customer, "SO20261020101 不想要了 退款")
    assert "已受理" in _final(events), _final(events)
    assert [r.status for r in _ledger("SO20261020101")] == ["failed", "processing"]


def test_an_order_system_outage_is_said_plainly_and_traced(remote, ask, login, no_backoff):
    remote.fail("GET", "/loopdesk/orders/", status=503, times=3)
    _, events = ask(login("customer@leon.demo"), "SO20261020101 这个订单什么状态")
    text = _final(events)
    assert "订单系统暂时无法访问" in text and "核对订单号" not in text, text
    span = _span(events, "get_order")
    [call] = span.output["integration"]
    assert span.status == "rejected" and (call["status"], call["attempts"]) == (503, 3), span.output
    assert len(no_backoff) == 2, "a pause before each retry"


def test_refunds_of_orders_in_the_order_system_count_toward_the_velocity_limit(remote, ask, login):
    # The limit is 3 automatic refunds per customer in 30 days. Emma's orders here exist only in the
    # order system, so the ledger has to know whose refund each one was without the demo tables.
    for n in range(4, 8):
        remote.add_order(f"SO2026102010{n}", "customer@leon.demo", 20.0)
    customer = login("customer@leon.demo")
    for n in range(4, 7):
        _, events = ask(customer, f"SO2026102010{n} 不想要了 退款")
        assert "已受理" in _final(events), _final(events)
    _, events = ask(customer, "SO20261020107 不想要了 退款")
    assert by_type(events, "approval_required"), _final(events)
    assert len(remote.refunds) == 3


def test_a_refused_refund_does_not_count_toward_the_velocity_limit(remote, settings, ask, login):
    settings.refund_window_max_count = 1
    remote.add_order("SO20261020104", "customer@leon.demo", 20.0)
    remote.refuse("SO20261020101", 409, "refund_declined", "held")
    customer = login("customer@leon.demo")
    _, events = ask(customer, "SO20261020101 不想要了 退款")
    assert _ledger("SO20261020101")[0].status == "failed"
    _, events = ask(customer, "SO20261020104 不想要了 退款")
    assert "已受理" in _final(events) and not by_type(events, "approval_required"), "nothing was paid the first time"


def test_jacks_earlier_refunds_still_count_when_his_order_lives_elsewhere(remote, ask, login):
    # Jack's three refunds this month were for orders in the demo tables (before the switch); a fourth,
    # for an order that lives only in the order system, still goes to a person.
    _, events = ask(login("customer2@leon.demo"), "SO20261020103 不想要了 退款")
    assert by_type(events, "approval_required"), _final(events)
    assert not remote.calls("POST", "/loopdesk/refunds")


def test_a_refund_waiting_for_confirmation_counts_as_made_for_the_sweep(remote, client, ask, login):
    # app.ops.stale_runs reopens an approval whose refund was not made. One the payment system has
    # not confirmed may have been made: it must not be reopened, or a second approval would file again.
    remote.fail("POST", "/loopdesk/refunds", status=503, times=3)
    _, events = ask(login("customer@leon.demo"), "SO20261020102 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"},
                    headers=login("admin@leon.demo"))
    assert r.status_code == 200 and "暂未" in r.json()["final"]["text"], r.text
    with new_session() as db:
        result = _refund_executed(db, approval_id)
    assert result is not None and result["refund_status"] == "pending", result


def test_staff_list_a_customers_orders_and_customers_only_their_own(remote, client):
    # The read behind the MCP server's loopdesk_list_orders: staff look a customer up by e-mail.
    services = get_services()

    def listed(email: str, customer_email: str) -> list[str]:
        with new_session() as db:
            user = db.execute(select(User).where(User.email == email)).scalar_one()
            tenant = db.get(Tenant, user.tenant_id)
            ctx = ToolContext(db=db, settings=services.settings, tenant=tenant, user=user, embedder=None,
                              run_id="list", conversation_id=None, orders=services.commerce.orders(tenant, db))
            return [o["order_id"] for o in list_orders(ctx, {"customer_email": customer_email})["orders"]]

    assert listed("agent@leon.demo", " Customer2@Leon.demo ") == ["SO20261020103"]
    assert listed("customer@leon.demo", "customer2@leon.demo") == ["SO20261020101", "SO20261020102"], "their own"
    assert [r.headers["x-loopdesk-customer"] for r in remote.calls("GET", "/loopdesk/orders")] == [
        "customer2@leon.demo", "customer@leon.demo"]


def test_evaluations_never_reach_the_merchants_systems(remote, client):
    with new_session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == "leon")).scalar_one()
        db.add(EvalCase(tenant_id=tenant.id, key="sandbox_order", suite="integration",
                        question="帮我查一下订单 SO20261008002", expected_tools=["get_order"],
                        must_include=["已发货", "1299.00"]))
        db.commit()
        result = run_suite(db, get_services(), tenant, suite="integration")
        assert result.passed == 1
    assert remote.requests == [], "the eval sandbox answers from its fixtures"


# ---------------------------------------------------------------- the demo tables
def test_the_demo_order_table_refuses_a_refund_for_an_order_changed_meanwhile(client, ask, login, monkeypatch):
    real = LocalRefundGateway.create_refund

    def changed_first(self, request):  # e.g. the order was refunded in the back office a moment ago
        self.db.execute(update(Order).where(Order.id == request.order_id).values(status="refunded"))
        return real(self, request)

    monkeypatch.setattr(LocalRefundGateway, "create_refund", changed_first)
    _, events = ask(login("customer@leon.demo"), "SO20260918001 不想要了 退款")
    assert "不能退款" in _final(events), _final(events)
    [refund] = _ledger("SO20260918001")
    assert refund.status == "failed" and refund.idempotency_key


def test_a_late_second_attempt_cannot_overwrite_a_settled_refund(settings, client, ask, login, monkeypatch):
    # Two attempts can hold the same pending row: the reconciliation job and a retried approval. The
    # demo order table refuses the second one (the order is "refunding" by then); that refusal must
    # not turn the refund the first attempt made into "failed" and free the order.
    real = LocalRefundGateway.create_refund

    def no_answer(self, request):
        raise GatewayError("refunds", "create_refund: ReadTimeout after 3 attempt(s)")

    monkeypatch.setattr(LocalRefundGateway, "create_refund", no_answer)
    _, events = ask(login("customer@leon.demo"), "SO20260918001 不想要了 退款")
    assert "暂未" in _final(events), _final(events)
    monkeypatch.setattr(LocalRefundGateway, "create_refund", real)
    with new_session() as late:
        refund = late.execute(select(Refund).where(Refund.order_id == "SO20260918001")).scalar_one()
        assert refund.status == "pending"  # what the late attempt read
        counts = reconcile_refunds(settings, commerce=get_services().commerce, older_than=timedelta(0))
        assert counts["confirmed"] == 1, counts
        file_refund(late, LocalRefundGateway(late, refund.tenant_id), refund)
        late.commit()
        assert refund.status == "processing", "the late attempt reports what happened"
    [refund] = _ledger("SO20260918001")
    assert refund.status == "processing" and refund.external_id == f"local-{refund.id}"


def test_local_refunds_go_through_the_same_ledger(client, ask, login):
    _, events = ask(login("customer@leon.demo"), "SO20260918001 不想要了 退款")
    assert "已受理" in _final(events)
    [refund] = _ledger("SO20260918001")
    assert (refund.status, refund.customer_id) == ("processing", _user_id("customer@leon.demo"))
    assert refund.idempotency_key and refund.external_id == f"local-{refund.id}"
    with new_session() as db:
        assert db.get(Order, "SO20260918001").status == "refunding"


def test_an_unconfirmed_refund_cannot_be_promised(settings):
    output = {"status": "auto_approved", "refund_status": "pending", "refund_id": 7,
              "message": "退款申请已提交（退款单 #7），支付系统暂未返回结果。"}
    evidence = [Evidence(ref=1, kind="tool", title="request_refund", text=json.dumps(output, ensure_ascii=False))]
    verification = verify_answer("退款会在 1-3 个工作日原路到账。", evidence, "退款", settings)
    assert verification.sentences[0].contradicts == "request_refund"


# ---------------------------------------------------------------- the HTTP contract
def _gateways(settings: Settings, fake: FakeCommerce, **conf):
    settings.commerce_apis = {"leon": CommerceApi(base_url=BASE, token=fake.token, **conf)}
    commerce = Commerce(settings, transport=fake.transport)
    tenant = SimpleNamespace(id=1, slug="leon")
    return commerce, commerce.orders(tenant, None), commerce.refunds(tenant, None)


def _request(key: str = "k-1") -> RefundRequest:
    return RefundRequest(idempotency_key=key, order_id="SO20261020101", amount=39.0, reason="不想要了",
                         approval_id=None, loopdesk_refund_id=1)


def test_calls_carry_the_token_the_tenant_the_request_id_and_a_signature(settings, no_backoff):
    fake = FakeCommerce(signing_secret="s" * 32)
    fake.add_order("SO20261020101", "customer@leon.demo", 39.0)
    _, orders, _ = _gateways(settings, fake, signing_secret="s" * 32)
    token = request_id_var.set("req-n028")
    try:
        info = orders.get_order("SO20261020101")
    finally:
        request_id_var.reset(token)
    [request] = fake.requests
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert (request.headers["x-loopdesk-tenant"], request.headers["x-request-id"]) == ("leon", "req-n028")
    assert (info.order_id, info.customer_ref, info.amount, info.status) == (
        "SO20261020101", "customer@leon.demo", 39.0, "delivered")
    assert info.delivered_at is not None and info.delivered_at.tzinfo is not None
    _, _, refunds = _gateways(settings, fake, signing_secret="s" * 32)
    assert refunds.create_refund(_request()).status == "processing", "the idempotency key is signed too"
    fake.signing_secret = "t" * 32  # the two sides disagree: refused, and a refusal is not retried
    with pytest.raises(GatewayError, match="401"):
        orders.get_order("SO20261020101")
    assert len(fake.requests) == 3


def test_reads_are_retried_and_give_up_after_the_configured_attempts(settings, no_backoff):
    fake = FakeCommerce()
    fake.add_order("SO20261020101", "customer@leon.demo", 39.0)
    _, orders, _ = _gateways(settings, fake, max_attempts=3)
    fake.fail("GET", "/loopdesk/orders/", status=503)
    assert orders.get_order("SO20261020101") is not None and len(fake.requests) == 2
    fake.fail("GET", "/loopdesk/orders/", error=httpx.ConnectTimeout, times=5)
    with pytest.raises(GatewayError) as failure:
        orders.get_order("SO20261020101")
    assert len(fake.requests) == 5 and failure.value.attempts == 3
    assert orders.get_order("SO20269999999") is None, "an unknown order is not an outage"


def test_only_a_post_with_an_idempotency_key_is_sent_again(settings, no_backoff):
    fake = FakeCommerce()
    commerce, _, _ = _gateways(settings, fake)
    fake.fail("POST", "/loopdesk/refunds", status=503, times=10)
    with pytest.raises(GatewayError):
        commerce.api("leon").request("POST", "/refunds", json={"order_id": "SO20261020101"})
    assert len(fake.requests) == 1
    with pytest.raises(GatewayError):
        commerce.api("leon").request("POST", "/refunds", json={"order_id": "SO20261020101"}, idempotency_key="k-9")
    assert len(fake.requests) == 1 + 3


def test_a_reply_that_breaks_the_contract_is_an_outage_not_a_crash(settings, no_backoff):
    fake = FakeCommerce()
    fake.add_order("SO20261020101", "customer@leon.demo", 39.0)
    fake.orders["SO20261020101"]["amount_fen"] = 39.5  # not whole fen
    _, orders, _ = _gateways(settings, fake)
    with pytest.raises(GatewayError, match="amount_fen"):
        orders.get_order("SO20261020101")
    assert len(fake.requests) == 1, "a malformed answer is not retried"


def test_refusals_and_replays_of_the_payment_system(settings, no_backoff):
    fake = FakeCommerce()
    fake.add_order("SO20261020101", "customer@leon.demo", 39.0)
    _, _, refunds = _gateways(settings, fake)
    first = refunds.create_refund(_request("k-1"))
    assert (first.refund_id, first.status) == ("RF000001", "processing")
    assert refunds.create_refund(_request("k-1")) == first and len(fake.refunds) == 1, "same key, same refund"
    with pytest.raises(RefundRejected) as refused:  # a new key for a refunded order: the order system says no
        refunds.create_refund(_request("k-2"))
    assert refused.value.code == "order_not_refundable"
    assert refunds.get_refund("RF000001").status == "processing"


def test_a_customers_orders_are_listed_without_putting_them_in_the_url(settings, no_backoff):
    fake = FakeCommerce()
    fake.add_order("SO20261020101", "customer@leon.demo", 39.0)
    fake.add_order("SO20261020103", "customer2@leon.demo", 45.0)
    _, orders, _ = _gateways(settings, fake)
    assert [o.order_id for o in orders.list_orders("customer@leon.demo")] == ["SO20261020101"]
    [request] = fake.requests
    assert "customer" not in str(request.url) and request.headers["x-loopdesk-customer"] == "customer@leon.demo"


def test_commerce_apis_are_set_per_tenant_and_refuse_plain_http(monkeypatch):
    conf = {"base_url": "https://oms.leon.example/api/loopdesk", "token": "t" * 24, "timeout": 3}
    monkeypatch.setenv("LOOPDESK_COMMERCE_APIS", json.dumps({"leon": conf}))
    api = Settings.from_env().commerce_apis["leon"]
    assert (api.base_url, api.timeout, api.max_attempts) == ("https://oms.leon.example/api/loopdesk", 3.0, 3)
    monkeypatch.setenv("LOOPDESK_COMMERCE_APIS", json.dumps({"leon": {**conf, "base_url": "http://oms.leon.example"}}))
    with pytest.raises(ValueError, match="https"):
        Settings.from_env()
    monkeypatch.setenv("LOOPDESK_COMMERCE_APIS", json.dumps({"leon": {**conf, "base_url": "http://127.0.0.1:9000"}}))
    assert Settings.from_env().commerce_apis["leon"].base_url == "http://127.0.0.1:9000"
    monkeypatch.setenv("LOOPDESK_COMMERCE_APIS", json.dumps({"leon": {"base_url": conf["base_url"]}}))
    with pytest.raises(ValueError, match="token"):
        Settings.from_env()


# ---------------------------------------------------------------- found in review (Claude Fable)
def _direct_refund(settings, email: str, args: dict) -> dict:
    """request_refund straight through the handler, as test_refund_race does."""
    services = get_services()
    with new_session() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one()
        tenant = db.get(Tenant, user.tenant_id)
        ctx = ToolContext(db=db, settings=settings, tenant=tenant, user=user, embedder=None, run_id="review",
                          conversation_id=None, orders=services.commerce.orders(tenant, db),
                          refunds=services.commerce.refunds(tenant, db))
        try:
            return request_refund(ctx, args)
        except ToolError as exc:
            return {"error": str(exc)}


def test_a_409_for_a_key_still_being_processed_is_not_a_refusal(remote, settings):
    # P0. Payment systems with standard idempotency answer 409 idempotency_key_in_use while the first
    # request under a key is still being made. LoopDesk's own retry can be that second request: the
    # first attempt timed out after the refund was made. Taking the 409 as "refused, nothing paid"
    # freed the order, and the next request paid again.
    remote.keep_delivered = True  # a partial refund leaves the order deliverable

    slowed: list[str] = []

    def made_then_slow(request):
        if request.method == "POST" and not slowed:
            slowed.append(request.headers["idempotency-key"])
            remote.handle(request)  # the refund is made ...
            remote.in_flight.add(slowed[0])  # ... and still being finalised when the retry comes
            raise httpx.ReadTimeout("slow", request=request)
        return remote.handle(request)

    get_services().commerce = Commerce(settings, transport=httpx.MockTransport(made_then_slow))
    first = _direct_refund(settings, "customer@leon.demo", {"order_id": "SO20261020101", "amount": 10, "reason": "少一件"})
    assert first.get("refund_status") == "pending", first
    assert len(remote.refunds) == 1
    second = _direct_refund(settings, "customer@leon.demo", {"order_id": "SO20261020101", "amount": 10, "reason": "少一件"})
    assert "已有一笔退款正在处理" in second.get("error", ""), second
    remote.in_flight.clear()
    assert reconcile_refunds(settings, commerce=get_services().commerce, older_than=timedelta(0))["confirmed"] == 1
    [refund] = _ledger("SO20261020101")
    assert (refund.status, refund.external_id) == ("processing", "RF000001")
    assert remote.refunded("SO20261020101") == 10.0, "paid once"


def test_a_409_or_422_with_a_code_outside_the_contract_keeps_the_order_held(remote, settings):
    remote.refuse("SO20261020101", 422, "rate_limited_try_later", "too many refunds this minute")
    out = _direct_refund(settings, "customer@leon.demo", {"order_id": "SO20261020101", "reason": "不想要了"})
    assert out.get("refund_status") == "pending", out
    [refund] = _ledger("SO20261020101")
    assert refund.status == "pending" and "rate_limited_try_later" in refund.error


def test_a_replay_sends_the_same_request_even_after_the_customer_was_erased(remote, settings, ask, login):
    # P1. The replay was rebuilt from the live user row; an erasure (or an e-mail change) between the
    # first send and the replay changed the body under the same key, and the payment system's
    # "key reused with a different request" freed the order of a refund that was made.
    from app.ops.retention import erase_user

    remote.fail("POST", "/loopdesk/refunds", error=httpx.ReadTimeout, after=True, times=3)  # made; replies lost
    _, events = ask(login("customer@leon.demo"), "SO20261020101 不想要了 退款")
    assert "暂未" in _final(events)
    [refund] = _ledger("SO20261020101")
    with new_session() as db:
        erase_user(db, refund.customer_id)
    assert reconcile_refunds(settings, commerce=get_services().commerce, older_than=timedelta(0))["confirmed"] == 1
    posts = remote.calls("POST", "/loopdesk/refunds")
    assert len({p.content for p in posts}) == 1, "every attempt sent the same body"
    assert _ledger("SO20261020101")[0].status == "processing" and len(remote.refunds) == 1


def test_an_order_system_outage_while_carrying_out_an_approval_puts_it_back(remote, client, ask, login):
    # P1. The re-validation read failed, the tool reported "订单系统暂时无法访问", and the approval stayed
    # "approved" with no refund behind it: the customer asking again needed a second approval.
    _, events = ask(login("customer@leon.demo"), "SO20261020102 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    admin = login("admin@leon.demo")
    remote.fail("GET", "/loopdesk/orders/", status=503, times=3)
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=admin)
    assert r.status_code == 503 and "已退回待处理" in r.json()["detail"], r.text
    with new_session() as db:
        assert db.get(Approval, approval_id).status == "pending"
    assert not remote.calls("POST", "/loopdesk/refunds")
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=admin)
    assert r.status_code == 200 and "已受理" in r.json()["final"]["text"], r.text
    assert len(remote.refunds) == 1


def test_a_refund_nobody_can_settle_becomes_a_ticket_once(remote, settings, ask, login):
    # P1. A 400 (or 401, 404) on every attempt kept the refund pending and the order held, and only a
    # warning in the log knew. After an hour it becomes a ticket in the staff workbench, once.
    from app.models import Ticket

    remote.fail("POST", "/loopdesk/refunds", status=400, code="bad_request", times=1000)
    _, events = ask(login("customer@leon.demo"), "SO20261020101 不想要了 退款")
    assert "暂未" in _final(events)
    commerce = get_services().commerce
    assert reconcile_refunds(settings, commerce=commerce, older_than=timedelta(0))["still_pending"] == 1
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    for _ in range(2):
        counts = reconcile_refunds(settings, commerce=commerce, now=later)
    assert counts["still_pending"] == 1, counts
    [refund] = _ledger("SO20261020101")
    with new_session() as db:
        tickets = db.execute(select(Ticket)).scalars().all()
    assert len(tickets) == 1 and refund.ticket_id == tickets[0].id, "one ticket, linked to the refund"
    assert f"#{refund.id}" in tickets[0].title and "HTTP 400" in tickets[0].description
    assert refund.attempts == 4, "the first send and three reconciliations"


def test_one_refund_per_order_and_a_finished_one_is_not_called_in_progress(remote, settings, ask, login):
    # P1. A partial refund is the order's one refund through the agent; once it succeeded, a second
    # request was told "已有一笔退款正在处理".
    remote.keep_delivered = True
    first = _direct_refund(settings, "customer@leon.demo", {"order_id": "SO20261020101", "amount": 10, "reason": "少一件"})
    assert first.get("refund_status") == "processing", first
    remote.refunds["RF000001"]["status"] = "succeeded"
    reconcile_refunds(settings, commerce=get_services().commerce, older_than=timedelta(0))
    [refund] = _ledger("SO20261020101")
    second = _direct_refund(settings, "customer@leon.demo", {"order_id": "SO20261020101", "amount": 10, "reason": "再退"})
    assert second == {"error": f"该订单已退款（退款单 #{refund.id}），每个订单只能在线申请一次退款，如需再退请转人工客服"}, second
    _, events = ask(login("customer@leon.demo"), "SO20261020101 这个订单什么状态")
    order = [e for e in by_type(events, "tool_result") if e["name"] == "get_order"][0]["output"]
    assert order["refundable"] is False and order["refund_note"].startswith("该订单已退款"), order


def test_a_refund_reads_the_order_once_and_calls_respect_the_run_deadline(remote, settings, ask, login, no_backoff):
    # P2. Precheck, policy and handler each read the order (four identical GETs per refund turn), and a
    # gateway call could outlast the run deadline: up to 60 s x 5 attempts was accepted.
    _, events = ask(login("customer@leon.demo"), "SO20261020101 不想要了 退款")
    assert "已受理" in _final(events)
    assert len(remote.calls("GET", "/loopdesk/orders/")) == 2, "the model's get_order, then one for the refund"
    with pytest.raises(ValueError, match="20"):
        CommerceApi(base_url=BASE, token=TOKEN, timeout=60, max_attempts=5)
    _, orders, _ = _gateways(settings, remote)
    remote.fail("GET", "/loopdesk/orders/", status=503, times=5)
    before = len(remote.requests)
    with commerce_deadline(time.monotonic() + 0.01):
        time.sleep(0.02)
        with pytest.raises(GatewayError):
            orders.get_order("SO20261020101")
    assert len(remote.requests) - before == 1, "past the run's deadline: one attempt, no retries"


def test_a_second_reconciliation_leaves_a_refund_another_one_is_filing(remote, settings, ask, login):
    # P2. Two overlapping runs (a CronJob allows that) both filed the same row.
    from app.ops.refund_reconcile import _lease

    remote.fail("POST", "/loopdesk/refunds", status=503, times=3)
    ask(login("customer@leon.demo"), "SO20261020101 不想要了 退款")
    [refund] = _ledger("SO20261020101")
    assert refund.attempts == 1
    cutoff = datetime.now(timezone.utc)  # both runs found it old enough
    with new_session() as db:
        assert _lease(db, refund.id, cutoff) is True
    with new_session() as db:
        assert _lease(db, refund.id, cutoff) is False, "the second run leaves it to the first"
    assert reconcile_refunds(settings, commerce=get_services().commerce)["still_pending"] == 0
    assert len(remote.calls("POST", "/loopdesk/refunds")) == 3


def test_reviewers_see_where_an_approved_refund_stands(remote, client, ask, login):
    remote.fail("POST", "/loopdesk/refunds", status=503, times=3)
    _, events = ask(login("customer@leon.demo"), "SO20261020102 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    admin = login("admin@leon.demo")
    client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=admin)
    decided = client.get("/api/approvals?status_filter=approved", headers=admin).json()
    [item] = [a for a in decided if a["id"] == approval_id]
    assert item["refund"]["status"] == "pending" and item["refund"]["order_id"] == "SO20261020102", item


def test_header_values_that_cannot_be_sent_are_an_outage_not_a_crash(settings, no_backoff):
    fake = FakeCommerce()
    _, orders, _ = _gateways(settings, fake)
    for customer in ("a\r\nX-Evil: 1@leon.demo", "顾客@leon.demo"):
        with pytest.raises(GatewayError):
            orders.list_orders(customer)
    assert fake.requests == []


def test_no_placeholder_request_id_and_no_secrets_in_the_config_repr(settings, no_backoff):
    fake = FakeCommerce()
    fake.add_order("SO20261020101", "customer@leon.demo", 39.0)
    _, orders, _ = _gateways(settings, fake, signing_secret="s" * 32)
    orders.get_order("SO20261020101")  # outside a request: request_id_var is "-"
    assert "x-request-id" not in fake.requests[0].headers
    assert TOKEN not in repr(settings.commerce_apis) and "s" * 32 not in repr(settings.commerce_apis)


def test_the_stale_run_cutoff_allows_for_the_order_and_payment_calls_of_a_resume():
    from app.ops.stale_runs import stale_after

    settings = Settings()
    api = CommerceApi(base_url=BASE, token=TOKEN, timeout=5, max_attempts=3)
    settings.commerce_apis = {"leon": api}
    assert api.call_seconds == 3 * (3 + 2 * 5) + 2 * 2  # connect, send and wait per attempt, pauses
    assert stale_after(settings) == timedelta(seconds=290 + 2 * api.call_seconds)


def test_the_demo_payment_system_keeps_tenants_apart(client):
    from sqlalchemy.exc import NoResultFound

    with new_session() as db:
        emma = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
        db.add(Refund(tenant_id=emma.tenant_id, order_id="SO20260918001", customer_id=emma.id, amount=1.0,
                      status="processing", idempotency_key="k-local", external_id="local-x"))
        db.commit()
        iris = db.execute(select(Tenant).where(Tenant.slug == "iris")).scalar_one()
        assert LocalRefundGateway(db, emma.tenant_id).get_refund("local-x").status == "processing"
        with pytest.raises(NoResultFound):
            LocalRefundGateway(db, iris.id).get_refund("local-x")


# ---------------------------------------------------------------- found in the re-review
def test_a_stuck_refund_stops_being_resent_and_a_person_settles_it(remote, settings, client, ask, login):
    # P2. A refund nobody could settle was sent again every run, forever, after its one ticket was
    # closed; past a payment system's key retention a re-send is a new request (a second payout for a
    # partial refund). Re-sends stop after 24 hours, a closed ticket on a still-pending refund opens a
    # new one, and an administrator records what the payment system's back office shows.
    from app.models import AuditLog, Ticket

    remote.fail("POST", "/loopdesk/refunds", status=400, code="bad_request", times=1000)
    conversation_id, events = ask(login("customer@leon.demo"), "SO20261020101 不想要了 退款")
    assert "暂未" in _final(events)
    commerce = get_services().commerce
    start = datetime.now(timezone.utc)
    assert reconcile_refunds(settings, commerce=commerce, now=start + timedelta(hours=2))["escalated"] == 1
    with new_session() as db:
        [ticket] = db.execute(select(Ticket)).scalars().all()
        assert ticket.conversation_id == conversation_id, "an automatic refund's ticket is in the customer's chat"
        ticket.status = "closed"  # closed without settling the refund
        db.commit()
    assert reconcile_refunds(settings, commerce=commerce, now=start + timedelta(hours=3))["escalated"] == 1
    sent = len(remote.calls("POST", "/loopdesk/refunds"))
    counts = reconcile_refunds(settings, commerce=commerce, now=start + timedelta(hours=25))
    assert len(remote.calls("POST", "/loopdesk/refunds")) == sent, "not sent again after 24 hours"
    assert counts["still_pending"] == 1 and counts["escalated"] == 0, counts
    [refund] = _ledger("SO20261020101")

    url = f"/api/refunds/{refund.id}/settle"
    body = {"outcome": "succeeded", "external_id": "RF-MANUAL-1", "note": "支付后台查到已原路退回"}
    assert client.post(url, json=body, headers=login("agent@leon.demo")).status_code == 403
    r = client.post(url, json=body, headers=login("admin@leon.demo"))
    assert r.status_code == 200 and r.json()["status"] == "succeeded", r.text
    assert client.post(url, json=body, headers=login("admin@leon.demo")).status_code == 409, "settled once"
    [refund] = _ledger("SO20261020101")
    assert (refund.status, refund.external_id) == ("succeeded", "RF-MANUAL-1")
    with new_session() as db:
        entry = db.execute(select(AuditLog).where(AuditLog.action == "refund.settle")).scalar_one()
        assert entry.detail["outcome"] == "succeeded" and entry.detail["note"] == body["note"]
    assert reconcile_refunds(settings, commerce=commerce, now=start + timedelta(hours=26))["still_pending"] == 0


def test_settling_a_stuck_refund_as_failed_frees_the_order(remote, settings, client, ask, login):
    remote.fail("POST", "/loopdesk/refunds", status=400, code="bad_request")  # not retried: one fault
    customer = login("customer@leon.demo")
    ask(customer, "SO20261020101 不想要了 退款")
    [refund] = _ledger("SO20261020101")
    with new_session() as db:  # past the 24 hours it is sent again: only a person can still settle it
        db.get(Refund, refund.id).created_at = datetime.now(timezone.utc) - timedelta(hours=25)
        db.commit()
    r = client.post(f"/api/refunds/{refund.id}/settle", headers=login("admin@leon.demo"),
                    json={"outcome": "failed", "note": "支付后台没有这笔退款"})
    assert r.status_code == 200 and r.json()["status"] == "failed", r.text
    _, events = ask(customer, "SO20261020101 不想要了 退款")
    assert "已受理" in _final(events), _final(events)


def test_one_bad_row_does_not_stop_the_reconciliation(remote, settings, ask, login, monkeypatch):
    from app.integrations.http import HttpRefundGateway

    remote.add_order("SO20261020104", "customer@leon.demo", 20.0)
    remote.fail("POST", "/loopdesk/refunds", status=503, times=6)
    customer = login("customer@leon.demo")
    ask(customer, "SO20261020101 不想要了 退款")
    ask(customer, "SO20261020104 不想要了 退款")
    [first] = _ledger("SO20261020101")
    real = HttpRefundGateway.create_refund

    def broken_for_the_first(self, request):
        if request.loopdesk_refund_id == first.id:
            raise RuntimeError("a bug")
        return real(self, request)

    monkeypatch.setattr(HttpRefundGateway, "create_refund", broken_for_the_first)
    counts = reconcile_refunds(settings, commerce=get_services().commerce, older_than=timedelta(0))
    assert (counts["errored"], counts["confirmed"]) == (1, 1), counts
    assert _ledger("SO20261020104")[0].status == "processing"
