"""A customer who does not know the order number (N-057): "我的快递咋还没到", "我最近买的那个扫地机器人到哪了",
"我要退款". The agent lists the customer's own orders, follows up on a single read-only match, and asks
which order before filing a refund; it never answers these from the policy documents."""

import pytest
from sqlalchemy import func, select

from app.agent.business_tools import list_my_orders
from app.agent.tools import ToolContext
from app.db import new_session
from app.services import get_services
from app.llm.mock import about_my_order, pick_my_order
from app.models import Approval, Refund, Tenant, User
from tests.conftest import by_type


def _tools(events: list[dict]) -> list[str]:
    return [e["name"] for e in by_type(events, "tool_result")]


def _final(events: list[dict]) -> str:
    return by_type(events, "final")[0]["text"]


def _refund_rows() -> int:
    with new_session() as db:
        return db.execute(select(func.count()).select_from(Refund)).scalar_one() + \
            db.execute(select(func.count()).select_from(Approval)).scalar_one()


@pytest.mark.parametrize("text", [
    "我要退款", "我的快递咋还没到啊，烦死了", "我最近买的那个扫地机器人到哪了", "快递到哪了",
    "我那个音箱的单子 退款进度怎么样了", "我买的那个扫地机器人退款", "我的退款到哪了",
])
def test_questions_about_my_order(text):
    assert about_my_order(text)


@pytest.mark.parametrize("text", [
    "退款多久到账", "能寄到香港吗", "收到货不满意能退吗", "SO20261008002 到哪了",
    "我的银行卡号是 6222 0212 3456 7890 123，退款能打到这张卡吗",
    "快递还没到能退款吗", "退货单子怎么填", "我买的东西能退货吗",
])
def test_policy_questions_and_numbered_orders_are_not(text):
    assert not about_my_order(text)


def test_pick_follows_only_a_single_match():
    listing = {"orders": [
        {"order_id": "A", "status": "shipped", "items": ["扫地机器人 X1"]},
        {"order_id": "B", "status": "shipped", "items": ["手冲咖啡套装"]},
        {"order_id": "C", "status": "delivered", "items": ["云朵抱枕"]},
    ]}
    assert pick_my_order("我买的扫地机器人到哪了", listing) == "A"
    assert pick_my_order("我的快递到哪了", listing) is None, "two parcels on the way: ask"
    assert pick_my_order("我的快递到哪了", {"orders": listing["orders"][::2]}) == "A"
    assert pick_my_order("我要退款", listing) is None


def test_lists_only_the_callers_orders_whatever_the_arguments(client):
    with new_session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == "leon")).scalar_one()
        jack = db.execute(select(User).where(User.email == "customer2@leon.demo")).scalar_one()
        ctx = ToolContext(db=db, settings=get_services().settings, tenant=tenant, user=jack, embedder=None,
                          run_id="mine", conversation_id=None)
        orders = list_my_orders(ctx, {"customer_email": "customer@leon.demo"})["orders"]
    ids = [o["order_id"] for o in orders]
    assert ids and "SO20261008002" not in ids, "Emma's orders stay hers"
    assert len(ids) <= 5


def test_my_parcel_is_tracked_without_a_number(ask, login):
    emma = login("customer@leon.demo")
    for _ in range(2):  # the second time must not come from the answer cache
        _, events = ask(emma, "我的快递咋还没到啊，烦死了")
        assert _tools(events)[:2] == ["list_my_orders", "get_order"] and "track_shipment" in _tools(events)
        assert "search_knowledge" not in _tools(events)
        assert "AT1402938475" in _final(events) and "Atlas快运" in _final(events)


def test_item_named_without_a_number(ask, login):
    _, events = ask(login("customer@leon.demo"), "我最近买的那个扫地机器人到哪了")
    assert "track_shipment" in _tools(events) and "AT1402938475" in _final(events)


def test_refund_without_a_number_asks_which_order(ask, login):
    before = _refund_rows()
    emma = login("customer@leon.demo")
    conversation, events = ask(emma, "我要退款")
    assert _tools(events) == ["list_my_orders"]
    assert not by_type(events, "approval_required") and _refund_rows() == before
    text = _final(events)
    assert "哪一单" in text and "SO20261008002" in text and "扫地机器人 X1" in text and "申请退款”" in text
    assert "SO20261003005" not in text, "another customer's order"
    _, events = ask(emma, "SO20260918001 申请退款", conversation_id=conversation)  # the reply the answer asks for
    assert "request_refund" in _tools(events)


@pytest.mark.parametrize("text", ["我买的那个扫地机器人退款", "我上次买的扫地机器人不想要了，退货", "我最近买的扫地机器人退钱"])
def test_no_refund_on_an_order_the_agent_picked(ask, login, text):
    # A single match by item name is enough to track a parcel, never to move money (review finding).
    before = _refund_rows()
    _, events = ask(login("customer@leon.demo"), text)
    assert _tools(events) == ["list_my_orders"] and not by_type(events, "approval_required")
    assert _refund_rows() == before and "哪一单" in _final(events)


def test_staff_are_not_given_the_customers_tool(ask, login):
    _, events = ask(login("agent@leon.demo"), "顾客问快递到哪了")
    assert "list_my_orders" not in _tools(events)


def test_refund_progress_by_item_name(ask, login):
    _, events = ask(login("customer2@leon.demo"), "我那个音箱的单子 退款进度怎么样了")
    assert "request_refund" not in _tools(events)
    assert "SO20261015009" in _final(events) and "没有查到在线提交的退款记录" in _final(events)


def test_policy_question_still_reads_the_knowledge_base(ask, login):
    _, events = ask(login("customer@leon.demo"), "退款多久到账")
    assert _tools(events) == ["search_knowledge"]
