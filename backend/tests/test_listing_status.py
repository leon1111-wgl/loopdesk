"""An order listing quotes each order's status (N-197): "SO…（音箱，已退款，¥59.00）" is the order tool's wording,
not a claim that this run refunded anything. A customer with refunded orders who says "我想退款" sees the list,
in strict mode too; a status the tools did not give that order is still a contradiction."""

import json

import pytest

from app.agent.status_templates import case_state, contradiction
from app.agent.tools import Evidence
from tests.conftest import by_type

LISTING = {"orders": [
    {"order_id": "SO20261010008", "status": "refunded", "status_text": "已退款", "amount": 59.0, "items": ["音箱"]},
    {"order_id": "SO20261015009", "status": "delivered", "status_text": "已签收", "amount": 199.0, "items": ["耳机"]},
]}


def _state(listing=LISTING):
    text = json.dumps(listing, ensure_ascii=False)
    return case_state([Evidence(ref=1, kind="tool", title="list_my_orders", text=text)], "我想退款")


def test_a_listed_status_is_not_a_refund_claim():
    state = _state()
    assert state.order_status["SO20261010008"] == "已退款"
    assert contradiction("SO20261010008（音箱，已退款，¥59.00）", state) is None


def test_a_status_the_listing_did_not_give_is_still_caught():
    state = _state()
    assert contradiction("SO20261015009（耳机，已退款，¥199.00）", state) == "request_refund"
    assert contradiction("您的退款已经到账了。", state) == "request_refund"


@pytest.mark.parametrize("mode", ["annotate", "strict"])
def test_customer_with_refunded_orders_sees_the_list(settings, ask, login, mode):
    settings.evidence_mode = mode
    _, events = ask(login("customer2@leon.demo"), "我想退款")
    assert "list_my_orders" in [e["name"] for e in by_type(events, "tool_result")]
    verification = by_type(events, "verification")[0]
    assert verification["verdict"] != "blocked", verification["sentences"]
    assert not [s for s in verification["sentences"] if s.get("contradicts")]
    assert not by_type(events, "answer_replaced")
    assert "已退款" in by_type(events, "final")[0]["text"]
