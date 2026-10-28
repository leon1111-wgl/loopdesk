"""A merchant's own order numbers (N-138).

With its own order system a tenant's orders are not "SO" + digits: Emma's are OMS0001 and OMS0002. The
format is declared with the system (order_id_pattern in LOOPDESK_COMMERCE_APIS); before, every rule that
looks for an order number in a customer's words (the offline model's tool choice, routing, the answer
cache, the verifier's order-number check) only knew the demo format, so "OMS0001 到哪了" listed the
customer's orders and asked which one, and Jack's question about Emma's OMS0001 was answered with his own.
"""

from types import SimpleNamespace

import pytest

from app.config import CommerceApi, get_settings
from app.integrations import Commerce
from app.services import get_services
from app.textutil import ORDER_ID_RE, refund_requested, withdraw_confirmed
from tests.fake_commerce import TOKEN, FakeCommerce

BASE = "https://oms.leon.test/loopdesk"


@pytest.fixture()
def oms(settings, client, monkeypatch):
    from app.integrations import http as commerce_http

    monkeypatch.setattr(commerce_http, "_sleep", lambda _s: None)
    fake = FakeCommerce()
    fake.add_order("OMS0001", "customer@leon.demo", 39.9, items=[{"name": "数据线", "qty": 1, "price": 39.9}])
    fake.add_order("OMS0002", "customer@leon.demo", 899.0, status="shipped", delivered_days_ago=None,
                   shipment={"carrier": "Orion快递", "tracking_no": "OR7000000002",
                             "events": [{"time": "2026-10-27 09:00", "desc": "派送中，快递员正在派件"}]})
    fake.add_order("OMS0003", "customer2@leon.demo", 45.0)
    settings.commerce_apis = {"leon": CommerceApi(base_url=BASE, token=TOKEN, order_id_pattern=r"OMS\d{4,10}")}
    get_services().commerce = Commerce(settings, transport=fake.transport)
    return fake


def _final(events: list[dict]) -> str:
    return next(e for e in events if e["type"] == "final")["text"]


def test_the_merchants_order_number_is_looked_up(oms, ask, login):
    _, events = ask(login("customer@leon.demo"), "OMS0001 这个订单什么状态")
    text = _final(events)
    assert "数据线" in text and "39.90" in text and "哪一单" not in text, text
    assert [r.url.path for r in oms.calls("GET", "/loopdesk/orders")] == ["/loopdesk/orders/OMS0001"]

    _, events = ask(login("customer@leon.demo"), "OMS0002 的快递到哪了")
    assert "OR7000000002" in _final(events), _final(events)


def test_a_refund_for_the_merchants_order_number_is_filed(oms, ask, login):
    _, events = ask(login("customer@leon.demo"), "OMS0001 申请退款")
    assert "已受理" in _final(events), _final(events)
    [post] = oms.calls("POST", "/loopdesk/refunds")
    assert b'"order_id": "OMS0001"' in post.content


def test_another_customers_order_number_is_not_answered_with_your_own(oms, ask, login):
    _, events = ask(login("customer2@leon.demo"), "OMS0001 到哪了")
    text = _final(events)
    assert "订单不存在或无权查看" in text and "OMS0003" not in text and "39.90" not in text, text
    assert oms.calls("GET", "/loopdesk/orders/OMS0001")


def test_the_rules_follow_the_declared_format(settings, client):
    get_settings().commerce_apis = {}
    assert not ORDER_ID_RE.search("OMS0001 到哪了"), "an undeclared format is not an order number"
    get_settings().commerce_apis = {"leon": CommerceApi(base_url=BASE, token=TOKEN, order_id_pattern=r"OMS\d{4,10}")}
    assert ORDER_ID_RE.findall("订单OMS0001和SO20261008002") == ["OMS0001", "SO20261008002"]
    assert not ORDER_ID_RE.search("OMS00012345678 OMS0001X XOMS0001"), "whole tokens only"
    assert refund_requested("OMS0001 我要退款") and withdraw_confirmed("确认撤回 OMS0001")
    assert refund_requested("太差了 OMS0001和OMS0002都退了吧")


def test_the_order_number_the_customer_typed_replaces_the_one_heard_and_is_spelled_out(settings, client):
    from app.voice.session import _substitute
    from app.voice.speakable import speakable

    get_settings().commerce_apis = {"leon": CommerceApi(base_url=BASE, token=TOKEN, order_id_pattern=r"OMS\d{4,10}")}
    typed = [SimpleNamespace(kind="order_id", value="OMS0002")]
    assert _substitute("OMS0001 和 OMS0003 到哪了", typed) == "OMS0002 和 OMS0003 到哪了"  # sub(count=1)
    assert "O M S，" in speakable("订单 OMS0001 已签收").text
    assert "S O，" in speakable("订单 SO20261008002 已签收").text


@pytest.mark.parametrize("pattern", [r"OMS[^\s]\d{4}", r"(?:OMS|WB)\d{6,12}", r"\d{16,20}"])
def test_ordinary_formats_are_accepted(pattern):
    CommerceApi(base_url=BASE, token=TOKEN, order_id_pattern=pattern)


@pytest.mark.parametrize("pattern", ["", r"(OMS)\d+", r"\d*", r"\d{4}", r"[一-鿿]+", "OMS[", "X" * 65,
                                     r"(?i)oms\d{4,10}", r"\d{5,}", r"\w{5,}", r".{5,}", r"(?:\d+)+Z",
                                     r"^OMS\d+$", r"\bOMS\d+"])
def test_a_pattern_that_would_match_too_much_or_break_the_rules_is_refused(pattern):
    with pytest.raises(ValueError, match="order_id_pattern"):
        CommerceApi(base_url=BASE, token=TOKEN, order_id_pattern=pattern)
