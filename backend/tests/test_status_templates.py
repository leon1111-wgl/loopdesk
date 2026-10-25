"""Status sentences come from the tools, not from the model (Parlant-style canned responses).

On the real-model run an answer said "退款将在 1-3 个工作日原路退回" after the refund was rejected: the
sentence is in the refund policy, so the evidence check passed it. Grounded is not correct for this
customer. A status sentence (refund filed, waiting for review, rejected, being confirmed, paid, failed;
a ticket's SLA) is now rendered from the tool's fields; the model writes only the framing around it,
and a status sentence of its own that contradicts the tool is blocked.
"""

import json

import pytest
from sqlalchemy import select

from app.agent.status_templates import case_state, claimed_states, contradiction, render_status
from app.agent.tools import Evidence
from app.db import new_session
from app.models import Approval, Message, Refund
from app.services import get_services
from tests.conftest import by_type
from tests.scripted_model import ScriptedModel, call


def tool(ref: int, name: str, output: dict) -> Evidence:
    return Evidence(ref, "tool", name, json.dumps(output, ensure_ascii=False))


FILED = tool(2, "request_refund", {"status": "auto_approved", "refund_status": "processing", "refund_id": 7,
                                   "order_id": "SO20260918001", "amount": 39.9,
                                   "message": "退款单 #7 已受理，退款金额 ¥39.90 将按原支付方式退回。"})
REJECTED = tool(2, "request_refund", {"status": "rejected", "comment": "缺少凭证",
                                      "message": "退款申请未通过人工审核。审核意见：缺少凭证。"})
PENDING = tool(2, "request_refund", {"status": "auto_approved", "refund_status": "pending", "refund_id": 9,
                                     "order_id": "SO20260918001", "amount": 39.9})
POLICY = Evidence(1, "kb", "售后与退款政策", "退款审核通过后按原支付方式原路退回。微信支付、支付宝预计1-3个工作日到账。")


# ---------------------------------------------------------------- rendering
def test_templates_render_from_tool_fields():
    assert render_status(case_state([FILED], "")).refund == "退款单 #7 已受理，退款金额 ¥39.90 将按原支付方式退回。"
    assert render_status(case_state([REJECTED], "")).refund == "退款申请未通过人工审核。审核意见：缺少凭证。"
    assert "支付系统暂未返回结果" in render_status(case_state([PENDING], "")).refund
    paid = tool(2, "request_refund", {"status": "approved", "refund_status": "succeeded", "refund_id": 3, "amount": 1299})
    assert render_status(case_state([paid], "")).refund == "退款单 #3 已退款成功，¥1299.00 已按原支付方式退回。"
    failed = tool(2, "request_refund", {"error": "该订单当前状态为“已退款”，不能发起退款"})
    assert render_status(case_state([failed], "")).refund == "退款申请未能提交：该订单当前状态为“已退款”，不能发起退款。"
    ticket = tool(1, "create_ticket", {"ticket_id": 5, "priority": "high", "sla_hours": 4})
    assert render_status(case_state([ticket], "")).ticket == "已为您创建工单 #5（优先级：高），技术支持将在 4 小时内联系您。"


def test_a_template_with_a_missing_field_is_not_used():
    no_id = tool(2, "request_refund", {"status": "auto_approved", "refund_status": "processing", "amount": 39.9})
    rendered = render_status(case_state([no_id], ""))
    assert rendered.refund is None and rendered.unusable == {"refund": ["refund_id"]}
    no_sla = tool(1, "create_ticket", {"ticket_id": 5, "priority": "normal"})
    assert render_status(case_state([no_sla], "")).ticket is None
    # a drill (eval dry run) has no refund id by design: it has its own template
    drill = tool(2, "request_refund", {"status": "auto_approved", "refund_id": None, "amount": 39.9,
                                       "message": "（演练）退款申请已受理，退款金额 ¥39.90 将按原支付方式退回。"})
    assert render_status(case_state([drill], ""), dry_run=True).refund.startswith("（演练）退款申请已受理")


# ---------------------------------------------------------------- what a sentence claims
@pytest.mark.parametrize("sentence, states", [
    ("您的退款已经到账了，请注意查收。", {"paid"}),
    ("退款将在1-3个工作日原路退回[1]。", {"will_pay"}),
    ("已为您提交退款申请。", {"submitted"}),
    ("您的退款申请已提交人工审核，请耐心等待。", {"submitted", "awaiting_review"}),
    ("很抱歉，您的退款申请未通过审核。", {"rejected"}),
    ("该订单不符合退款条件，无法为您退款。", {"not_refundable"}),
    ("退款不会到账，因为审核没有通过。", {"rejected"}),
    ("退款审核通过后会按原支付方式退回。", set()),  # a condition, not a status
    ("如果申请退款，款项会原路退回。", set()),
    ("请提供订单号，我帮您查询退款进度。", set()),
])
def test_what_a_sentence_says_about_the_refund(sentence, states):
    assert claimed_states(sentence).refund == states


@pytest.mark.parametrize("evidence, sentence, contradicts", [
    ([POLICY, REJECTED], "退款将在1-3个工作日原路退回[1]。", True),   # the real-model case: grounded, wrong
    ([POLICY, REJECTED], "很抱歉，您的退款申请未通过审核。", False),
    ([POLICY, PENDING], "您的退款已受理，将原路退回。", True),          # not confirmed by the payment system
    ([POLICY, PENDING], "退款申请已提交，结果以订单状态为准。", False),
    ([POLICY, FILED], "您的退款已经到账了。", True),                    # filed is not paid
    ([POLICY, FILED], "退款将按原支付方式退回，预计1-3个工作日到账[1]。", False),
    ([POLICY], "已为您提交退款申请，请耐心等待。", True),                # no tool filed anything
    ([POLICY], "退款审核通过后按原支付方式原路退回[1]。", False),         # policy, not a status
])
def test_a_status_sentence_is_checked_against_the_tool_state(evidence, sentence, contradicts):
    assert bool(contradiction(sentence, case_state(evidence, "SO20260918001 我要退款"))) is contradicts


def test_deciding_a_refund_without_the_tool_is_a_contradiction():
    """Insight 2: the model judged "不能退" itself and never called request_refund, so the tool's checks and
    the approval queue never saw the request, and the answer read as compliant."""
    order = tool(1, "get_order", {"order_id": "SO20260918001", "status": "delivered", "refundable": True,
                                  "refund_note": "", "amount": 39.9})
    asked = case_state([order], "SO20260918001 我要退款")
    assert contradiction("很抱歉，该订单不符合退款条件，无法为您退款。", asked) == "under_action"
    # the order tool said no: repeating it is consistent
    closed = tool(1, "get_order", {"order_id": "SO20260918001", "status": "refunded", "refundable": False,
                                   "refund_note": "该订单当前状态为“已退款”，不能重复申请退款"})
    assert not contradiction("该订单已退款，不能重复申请退款。", case_state([closed], "SO20260918001 我要退款"))
    # a question about the policy is not a request: answering "不能" is not under-action
    assert not contradiction("定制商品不支持七天无理由退货[1]。", case_state([order], "定制的能退吗"))


def test_claiming_a_ticket_or_a_handoff_no_tool_made_is_a_contradiction():
    state = case_state([POLICY], "扫地机器人坏了")
    assert contradiction("已为您创建工单，技术支持会尽快联系您。", state) == "ticket"
    assert contradiction("已为您转接人工客服，请稍候。", state) == "handoff"
    ticket = tool(2, "create_ticket", {"ticket_id": 5, "priority": "normal", "sla_hours": 24})
    assert not contradiction("已为您创建工单，技术支持会尽快联系您。", case_state([POLICY, ticket], "扫地机器人坏了"))


# ---------------------------------------------------------------- in the agent loop
def _stored_meta(run_id: str) -> dict:
    """What staff see on the answer (customers get it without the status details)."""
    with new_session() as db:
        return db.execute(select(Message).where(Message.run_id == run_id, Message.role == "assistant")
                          .order_by(Message.id.desc())).scalars().first().meta


def _use(monkeypatch, *steps) -> ScriptedModel:
    model = ScriptedModel(*steps)
    monkeypatch.setattr(get_services(), "provider", model)
    return model


def test_the_refund_status_sentence_is_the_template_not_the_model(ask, login, monkeypatch):
    _use(monkeypatch, call("get_order", order_id="SO20260918001"),
         call("request_refund", order_id="SO20260918001", amount=39.9, reason="不想要了"),
         "好的，您的退款已经到账了，请注意查收。如有其他问题欢迎随时联系。")
    _, events = ask(login("customer@leon.demo"), "SO20260918001 我要退款")
    final = by_type(events, "final")[0]
    with new_session() as db:
        refund = db.execute(select(Refund).where(Refund.order_id == "SO20260918001")).scalar_one()
    assert final["text"] == (f"退款单 #{refund.id} 已受理，退款金额 ¥39.90 将按原支付方式退回。"
                             "如有其他问题欢迎随时联系。")
    assert not any("到账" in e["text"] for e in by_type(events, "token")), "the model's status sentence never streams"
    assert "status_sentences" not in final["meta"], "customers are not shown the model's replaced wording"
    status = _stored_meta(final["run_id"])["status_sentences"]
    assert status["rendered"] == ["refund"] and status["replaced"] == ["好的，您的退款已经到账了，请注意查收。"]
    assert status["blocked"] == ["好的，您的退款已经到账了，请注意查收。"], "it contradicted the tool: logged as blocked"


def test_after_a_rejection_the_customer_hears_the_rejection(client, ask, login, monkeypatch):
    customer = login("customer@leon.demo")
    _use(monkeypatch, call("get_order", order_id="SO20261008002"),
         call("request_refund", order_id="SO20261008002", amount=1299, reason="质量问题"),
         "很抱歉，您的退款申请未通过审核。退款将在1-3个工作日原路退回。如有疑问可以回复“转人工”。")
    _, events = ask(customer, "SO20261008002 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "reject", "comment": "缺少凭证"},
                    headers=login("admin@leon.demo"))
    text = r.json()["final"]["text"]
    assert text == "退款申请未通过人工审核。审核意见：缺少凭证。如有疑问可以回复“转人工”。"
    with new_session() as db:
        assert db.get(Approval, approval_id).status == "rejected"


def test_without_its_fields_the_template_is_skipped_and_the_model_sentence_checked(ask, login, monkeypatch):
    from app.agent import business_tools

    original = business_tools._refund_result

    def without_id(refund, status):
        result = original(refund, status)
        result.pop("refund_id")
        return result

    monkeypatch.setattr(business_tools, "_refund_result", without_id)
    _use(monkeypatch, call("request_refund", order_id="SO20260918001", amount=39.9, reason="不想要了"),
         "您的退款已经到账了。")
    _, events = ask(login("customer@leon.demo"), "SO20260918001 我要退款")
    final = by_type(events, "final")[0]
    assert _stored_meta(final["run_id"])["status_sentences"]["unusable"] == {"refund": ["refund_id"]}
    check = by_type(events, "verification")[0]
    assert check["verdict"] == "blocked" and check["sentences"][0]["contradicts"] == "request_refund"


def test_a_ticket_sla_comes_from_the_ticket(ask, login, monkeypatch):
    _use(monkeypatch, call("create_ticket", title="扫地机器人坏了", description="开不了机", priority="normal"),
         "工单已提交，技术支持将在 2 小时内联系您。给您带来不便非常抱歉。")
    _, events = ask(login("customer@leon.demo"), "我的扫地机器人坏了，开不了机")
    text = by_type(events, "final")[0]["text"]
    assert "24 小时内联系您" in text and "2 小时" not in text.replace("24 小时", "")
    assert text.startswith("已为您创建工单 #") and text.endswith("给您带来不便非常抱歉。")


# ---------------------------------------------------------------- shipment status
UNSHIPPED = tool(1, "get_order", {"order_id": "SO20261012003", "status": "paid", "status_text": "已付款，待发货",
                                  "refundable": True})
DELIVERING = tool(2, "track_shipment", {"order_id": "SO20261008002", "shipped": True, "carrier": "Atlas快运",
                                        "events": [{"time": "2026-10-20 17:57", "desc": "派送中，快递员正在派件"}],
                                        "message": "订单 SO20261008002 由Atlas快运承运，运单号 AT1。最新进度（2026-10-20 17:57）：派送中，快递员正在派件。"})


@pytest.mark.parametrize("evidence, sentence, contradicts", [
    ([UNSHIPPED], "您的订单已发货，请留意物流信息。", "shipment"),
    ([UNSHIPPED], "您的订单目前还未发货，暂无物流信息。", None),
    ([DELIVERING], "包裹已签收，请确认商品是否完好。", "shipment"),
    ([DELIVERING], "包裹正在派送中，请保持电话畅通。", None),
    ([DELIVERING], "订单还没有发货。", "shipment"),
    ([POLICY], "现货商品付款后 48 小时内发货。", None),  # policy, no order looked up
])
def test_a_shipment_status_is_checked_against_the_order_and_tracking(evidence, sentence, contradicts):
    assert contradiction(sentence, case_state(evidence, "我的快递到哪了")) == contradicts


def test_no_refund_after_a_failed_order_lookup_is_not_under_action():
    missing = tool(1, "get_order", {"error": "订单不存在或无权查看"})
    state = case_state([missing], "SO20261003005 我要退款")
    assert contradiction("抱歉，未查询到订单 SO20261003005，因此暂时无法发起退款。", state) is None
