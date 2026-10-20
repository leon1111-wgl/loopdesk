"""Echo音乐, the music-streaming tenant (SYNTHETIC): memberships, coins and digital albums are
virtual orders. They take effect on payment, the refund windows count from the charge, nothing
is shipped, and some items cannot be refunded online at all. The e-commerce wording stays as it was."""

from app.db import new_session
from app.models import Approval
from app.services import get_services
from tests.conftest import by_type
from tests.test_refund_policy import ScriptedRefundModel


def results(events, name: str) -> list[dict]:
    return [e["output"] for e in by_type(events, "tool_result") if e["name"] == name]


def test_a_membership_order_reads_as_opened_not_signed(ask, login):
    _, events = ask(login("customer@echo.demo"), "SO20261014103 是什么订单？")
    order = results(events, "get_order")[0]
    assert order["status_text"] == "已开通" and order["refundable"]
    assert order["refund_note"] == ("订单已开通 5 天，已超过 3 天无理由退款期限，7 天内需核实本期权益未使用后才能退款，"
                                    "提交后需要人工核实")


def test_membership_refunds_follow_the_windows_counted_from_the_charge(ask, login):
    lily = login("customer@echo.demo")
    _, events = ask(lily, "SO20261019101 刚被扣了续费，帮我退款")  # ¥18, charged yesterday
    assert results(events, "request_refund")[0]["status"] == "auto_approved" and not by_type(events, "approval_required")

    _, events = ask(lily, "SO20261015102 年卡我要退款")  # ¥198: over the automatic limit (the reviewer is told)
    with new_session() as db:
        assert "超过自动审批额度" in db.get(Approval, by_type(events, "approval_required")[0]["approval_id"]).reason

    _, events = ask(lily, "SO20261014103 我要退款")  # day 5: a person checks the benefits were unused
    assert "订单已开通 5 天，超过 3 天无理由退款期限" in by_type(events, "approval_required")[0]["reason"]

    _, events = ask(lily, "SO20261001104 申请退款")  # day 19: past every window
    order = results(events, "get_order")[0]
    assert not order["refundable"] and order["refund_note"] == "订单已开通 19 天，超过 7 天的售后期限"
    assert not results(events, "request_refund")


def test_items_that_cannot_be_refunded_online_say_why(ask, login, monkeypatch):
    lily = login("customer@echo.demo")
    for order_id, why in (("SO20261018105", "监护人"), ("SO20261012106", "重复购买"), ("SO20261016107", "Apple")):
        _, events = ask(lily, f"{order_id} 我要退款")
        order = results(events, "get_order")[0]
        assert not order["refundable"] and why in order["refund_note"], order_id
        assert not results(events, "request_refund"), order_id
    # a model that calls the tool anyway gets the same reason, and nothing is filed
    monkeypatch.setattr(get_services(), "provider", ScriptedRefundModel({"order_id": "SO20261018105", "amount": 98}))
    _, events = ask(lily, "SO20261018105 我要退款")
    assert "监护人" in results(events, "request_refund")[0]["error"] and not by_type(events, "approval_required")
    monkeypatch.setattr(get_services(), "provider", ScriptedRefundModel({"order_id": "SO20261001104"}))
    _, events = ask(lily, "SO20261001104 我要退款")
    assert results(events, "request_refund")[0]["error"].startswith("订单已开通 19 天，超过 7 天的售后期限，不能在线申请退款")


def test_a_virtual_order_has_no_logistics(ask, login):
    _, events = ask(login("customer@echo.demo"), "SO20261019101 发货了吗？")
    message = results(events, "track_shipment")[0]["message"]
    assert "虚拟商品" in message and "没有物流" in message and "还未发货" not in message


def test_goods_that_are_shipped_keep_their_wording(ask, login):
    _, events = ask(login("customer@leon.demo"), "SO20261008010 我要退款")
    assert by_type(events, "approval_required")[0]["reason"].startswith(
        "订单已签收 10 天，超过 7 天无理由退货期限，15 天内仅质量问题可以退换")


def test_another_customers_and_another_tenants_orders_are_not_found(ask, login):
    lily = login("customer@echo.demo")
    for order_id in ("SO20261010108", "SO20261008002"):  # Ryan's annual card; a Leon优选 order
        _, events = ask(lily, f"帮我查一下 {order_id}")
        assert results(events, "get_order")[0]["error"] == "订单不存在或无权查看", order_id
