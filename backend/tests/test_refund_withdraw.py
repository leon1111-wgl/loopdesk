"""A customer takes back a refund application that is still waiting for review (N-127), in two steps: the
first turn says what withdrawing means and asks for "确认撤回"; only that reply takes it back (N-145)."""

import pytest
from sqlalchemy import func, select

from app.db import new_session
from app.models import Approval, AuditLog, Order, Refund, Run
from app.textutil import refund_requested, refund_withdraw_requested
from tests.conftest import by_type

ORDER = "SO20261008002"  # ¥1299, over Leon's automatic limit: every refund on it waits for review


def _file(ask, login) -> tuple[dict, int]:
    customer = login("customer@leon.demo")
    _, events = ask(customer, f"{ORDER} 我要退款")
    return customer, by_type(events, "approval_required")[0]["approval_id"]


def _final(events: list[dict]) -> dict:
    return by_type(events, "final")[0]


def _tools(events: list[dict]) -> list[str]:
    return [e["name"] for e in by_type(events, "tool_call")]


def _withdraw(ask, customer, text: str, confirm: str = "确认撤回") -> list[dict]:
    """Ask, check that nothing was taken back yet, then confirm in the same conversation."""
    conversation, events = ask(customer, text)
    assert "withdraw_refund_request" in _tools(events) and "request_refund" not in _tools(events)
    offer = _final(events)["text"]
    assert "确认撤回" in offer and "还没有撤回" in offer and "已为您撤回" not in offer, offer
    _, events = ask(customer, confirm, conversation)
    return events


@pytest.mark.parametrize("text", [
    f"{ORDER} 我刚才的退款申请能撤回吗 不想退了",  # the nitpick's words
    f"取消退款申请 {ORDER}",
    f"撤回 {ORDER} 的退款申请",
    f"麻烦把{ORDER}的退款申请撤销一下",
    f"cancel my refund request {ORDER}",
    f"{ORDER} 不想退了",  # no verb, but an application is waiting: that is what it means
    # the reviewer's phrasings that the first version still read as a refund request
    "帮我把退款申请撤了",
    f"把刚才{ORDER}的退款取消掉",
    f"{ORDER} 退款申请我要取消",
    f"{ORDER} 退款申请不要了",
    f"revoke my refund request {ORDER}",
])
def test_withdrawing_in_another_conversation_takes_the_application_off_the_queue(client, ask, login, text):
    customer, approval_id = _file(ask, login)
    if ORDER not in text:
        text = f"{ORDER} {text}"
    events = _withdraw(ask, customer, text)  # a new conversation, as in the report
    assert "withdraw_refund_request" in _tools(events) and "request_refund" not in _tools(events)
    assert "已为您撤回" in _final(events)["text"] and "¥1299.00" in _final(events)["text"]

    admin = login("admin@leon.demo")
    assert [a["id"] for a in client.get("/api/approvals", headers=admin).json()] == []
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=admin)
    assert r.status_code == 409 and "顾客已撤回" in r.json()["detail"]
    with new_session() as db:
        approval = db.get(Approval, approval_id)
        assert approval.status == "withdrawn" and approval.reviewer_id is None and approval.decided_at
        assert db.get(Run, approval.run_id).status == "completed"
        assert db.execute(select(func.count(Refund.id)).where(Refund.order_id == ORDER)).scalar_one() == 0
        assert db.get(Order, ORDER).status == "shipped"
        entry = db.execute(select(AuditLog).where(AuditLog.action == "approval.withdraw")).scalar_one()
        assert entry.target == f"approval:{approval_id}" and entry.detail["amount"] == 1299.0


def test_the_conversation_that_said_under_review_hears_the_outcome(client, ask, login):
    customer = login("customer@leon.demo")
    first, events = ask(customer, f"{ORDER} 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    _withdraw(ask, customer, f"取消退款申请 {ORDER}")
    last = client.get(f"/api/conversations/{first}/messages", headers=customer).json()["messages"][-1]
    assert last["role"] == "assistant" and "您已撤回" in last["content"]
    assert last["meta"]["status"] == "completed", "the page stops polling for a decision"
    with new_session() as db:
        run = db.get(Run, db.get(Approval, approval_id).run_id)
        assert "pending" not in (run.state or {}) and "您已撤回" in run.output_text


@pytest.mark.parametrize("text", [
    f"{ORDER} 先别退款，如果退款的话钱退到哪",
    f"{ORDER} 暂时不退款，退款是原路退回吗",
    f"为什么取消我的退款申请 {ORDER}",
    f"取消退款申请以后还能再申请吗 {ORDER}",
    f"{ORDER} 退款到哪一步了",
])
def test_a_question_or_complaint_does_not_take_the_waiting_application_back(client, ask, login, text):
    customer, approval_id = _file(ask, login)
    _, events = ask(customer, text)
    assert "withdraw_refund_request" not in _tools(events) and "已为您撤回" not in _final(events)["text"]
    with new_session() as db:
        assert db.get(Approval, approval_id).status == "pending"


def test_withdrawn_shows_in_the_full_list_and_the_order_can_be_refunded_again(client, ask, login):
    customer, approval_id = _file(ask, login)
    _withdraw(ask, customer, f"取消退款申请 {ORDER}")
    admin = login("admin@leon.demo")
    listed = client.get("/api/approvals?status_filter=all", headers=admin).json()
    assert [(a["id"], a["status"]) for a in listed] == [(approval_id, "withdrawn")]
    _, events = ask(customer, f"{ORDER} 我要退款")  # changed their mind again: a fresh application
    assert by_type(events, "approval_required")


def test_nothing_waiting_says_so_and_a_filed_refund_cannot_be_withdrawn(client, ask, login):
    customer = login("customer@leon.demo")
    _, events = ask(customer, f"取消退款申请 {ORDER}")
    assert "没有等待审核的退款申请" in _final(events)["text"]

    _, approval_id = _file(ask, login)
    admin = login("admin@leon.demo")
    assert client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=admin).status_code == 200
    _, events = ask(customer, f"撤回 {ORDER} 的退款申请")
    text = _final(events)["text"]
    assert "已提交到支付系统" in text and "无法在线撤回" in text and "已为您撤回" not in text


def test_only_the_customers_own_application_and_never_on_a_refund_request(client, ask, login):
    _, approval_id = _file(ask, login)
    other = login("customer2@leon.demo")  # someone else naming the same order
    _, events = ask(other, f"取消退款申请 {ORDER}")
    assert "已为您撤回" not in _final(events)["text"]
    with new_session() as db:
        assert db.get(Approval, approval_id).status == "pending"


def test_the_tool_refuses_a_turn_that_asks_for_or_about_a_refund(client, login):
    """Whatever a model makes of "我要退款" or "退款到哪了", the tool layer does not take an application back."""
    from app.agent.business_tools import withdraw_refund_request
    from app.agent.tools import ToolContext, ToolError
    from app.models import Tenant, User
    from app.services import get_services

    customer = login("customer@leon.demo")
    client.post("/api/conversations", json={}, headers=customer)
    services = get_services()
    with new_session() as db:
        user = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
        tenant = db.get(Tenant, user.tenant_id)
        for text in (f"{ORDER} 我要退款", f"{ORDER} 退款到哪一步了", f"{ORDER} 退了吧",
                     f"{ORDER} 先别退款，如果退款的话钱退到哪"):
            ctx = ToolContext(db=db, settings=services.settings, tenant=tenant, user=user, embedder=services.embedder,
                              run_id="t", conversation_id=None, customer_text=text)
            with pytest.raises(ToolError):
                withdraw_refund_request(ctx, {"order_id": ORDER})


@pytest.mark.parametrize("text,expected", [
    ("SO20261008010 我刚才的退款申请能撤回吗 不想退了", True),
    ("取消退款申请 SO20261008010", True),
    ("撤回 SO20261008010 的退款", True),
    ("退款申请可以取消吗", True),  # read as one, but it only gets the offer: nothing goes before "确认撤回" (N-145)
    ("Please withdraw the refund for SO20261008010", True),
    ("我不是要撤回退款申请", False),
    ("别取消退款申请", False),
    ("SO20261008010 我要退款", False),
    ("取消订单", False),
    ("退款被取消了吗", False),
    ("帮我把退款申请撤了", True),
    ("退款申请怎么取消", True),
    ("不退了，撤回申请", True),
    ("退款申请作废", True),
    ("为什么取消我的退款申请", False),
    ("取消退款申请后钱会退吗", False),
    ("退款申请取消了，怎么回事", False),
    ("取消退款申请就算了，继续退吧", False),
    ("我没有取消退款申请", False),
    ("why did you cancel my refund?", False),
    ("取消订单并退款", False),
    ("取消自动续费并退款", False),
    # second review round
    ("算了，撤回SO20261008002的退款申请吧", True),
    ("退款申请能撤吗", True),
    ("取消下退款申请", True),
    ("取消退款申请，我还是要退", False),
    # N-145: keeping it
    ("SO20261008002 退款申请撤回不了吧 我还要退的", False),
    ("SO20261008002 cancel the refund? no, keep it", False),
])
def test_withdraw_intent(text, expected):
    assert refund_withdraw_requested(text) is expected
    if expected:
        assert not refund_requested(text), "taking one back is never a request to file one"


def test_the_verifier_knows_a_withdrawal():
    """A real model's own words for it pass; a stale "still under review" does not (reviewed after N-127)."""
    import json

    from app.agent.status_templates import case_state, contradiction
    from app.agent.tools import Evidence

    order = {"order_id": ORDER, "status": "shipped", "refundable": False, "refunds": [],
             "refund_awaiting_approval": {"approval_id": 1, "amount": 1299.0}}
    done = {"withdrawn": True, "order_id": ORDER, "amount": 1299.0, "message": "已为您撤回"}
    evidence = [Evidence(ref=1, kind="tool", title="get_order", text=json.dumps(order), meta={}),
                Evidence(ref=2, kind="tool", title="withdraw_refund_request", text=json.dumps(done), meta={})]
    state = case_state(evidence, f"撤回 {ORDER} 的退款申请")
    assert contradiction("好的，您的退款申请已经撤回，这笔订单不会退款。", state) is None
    assert contradiction("您的退款申请还在人工审核中。", state) == "request_refund"
    assert contradiction("您的退款申请已经撤回。", case_state(evidence[:1], f"{ORDER} 退款到哪了")) == "request_refund"
    # cancelling something else is no claim about a refund (Echo's 取消自动续费 flow)
    for sentence in ("您的自动续费已取消，下个周期不再扣费。", "已为您取消自动续费。", "已为您取消订单。"):
        assert contradiction(sentence, case_state([], "怎么取消自动续费")) is None


# N-145: the nitpick's questions. Each gets the offer (or an answer) and nothing is taken back.
@pytest.mark.parametrize("text", [
    f"{ORDER} 退款申请 取消了吗",
    f"{ORDER} 是不是把我的退款申请撤回了",
    f"{ORDER} 撤销退款申请需要什么条件",
    f"{ORDER} 退款申请 能撤回吗？先问问",
    f"{ORDER} 退款申请撤回会影响信用吗",
    f"{ORDER} 退款申请撤回不了吧 我还要退的",
    f"{ORDER} cancel the refund? no, keep it",
    f"{ORDER} 我先撤回退款，再重新申请部分退款 500 元",
    f"{ORDER} 退款申请撤回的话 还能再申请吗",
    f"{ORDER} 退款申请可以取消吗",
    f"{ORDER} 撤回退款申请",  # an instruction too: it is read back first
])
def test_one_turn_never_takes_the_application_back(client, ask, login, text):
    customer, approval_id = _file(ask, login)
    _, events = ask(customer, text)
    assert "已为您撤回" not in _final(events)["text"]  # "我还要退的" may try to file again: the waiting one blocks it
    with new_session() as db:
        assert db.get(Approval, approval_id).status == "pending"
        assert db.execute(select(func.count(Approval.id))).scalar_one() == 1
        assert db.execute(select(func.count(AuditLog.id)).where(AuditLog.action == "approval.withdraw")).scalar_one() == 0


@pytest.mark.parametrize("reply", ["确认撤回", "撤回吧", "确认撤回订单SO20261008002的退款申请",
                                   "确认撤回吗？", "确认撤回 SO20261008010", "不用了"])
def test_only_a_confirmation_of_this_offer_takes_it_back(client, ask, login, reply):
    """"确认撤回" confirms; a question, another order or a no does not."""
    customer, approval_id = _file(ask, login)
    conversation, _ = ask(customer, f"{ORDER} 撤回退款申请")
    ask(customer, reply, conversation)
    with new_session() as db:
        assert db.get(Approval, approval_id).status == ("pending" if "吗" in reply or "SO20261008010" in reply or reply == "不用了" else "withdrawn"), reply


def test_an_offer_does_not_carry_past_the_next_answer_or_into_another_conversation(client, ask, login):
    customer, approval_id = _file(ask, login)
    conversation, _ = ask(customer, f"{ORDER} 撤回退款申请")
    ask(customer, "退款一般多久到账", conversation)  # something else in between
    ask(customer, "确认撤回", conversation)
    _, events = ask(customer, "确认撤回")  # a new conversation never saw the offer
    with new_session() as db:
        assert db.get(Approval, approval_id).status == "pending"


def test_a_confirmation_takes_back_only_what_was_offered(client, ask, login):
    """The tool, driven directly as a real model would: confirming withdraws the offered approval ids."""
    from app.agent.business_tools import withdraw_refund_request
    from app.agent.tools import ToolContext
    from app.models import Tenant, User
    from app.services import get_services

    customer, approval_id = _file(ask, login)
    conversation, events = ask(customer, f"{ORDER} 撤回退款申请")
    services = get_services()
    with new_session() as db:
        user = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
        tenant = db.get(Tenant, user.tenant_id)

        def call(text: str, args: dict) -> dict:
            ctx = ToolContext(db=db, settings=services.settings, tenant=tenant, user=user,
                              embedder=services.embedder, run_id="t", conversation_id=conversation, customer_text=text)
            return withdraw_refund_request(ctx, args)

        other = call("确认撤回", {"order_id": "SO20261008010"})  # a different order than the one offered
        assert other["withdrawn"] is False
        named = call("确认撤回 SO20261008010", {})  # the customer named another order; the model passed none (review)
        assert named["withdrawn"] is False
        kept = call("不用了", {"order_id": ORDER})
        assert kept.get("kept") and "不撤回" in kept["message"] and not kept.get("confirm_required")
        for restated in ("取消退款申请", "不退了"):  # the withdrawal said again is not a no (review)
            again = call(restated, {"order_id": ORDER})
            assert not again.get("kept") and again["withdrawn"] is False, restated
        assert db.get(Approval, approval_id).status == "pending"
        done = call("好的", {})  # a yes the voice read-back accepts, as a real model would pass it on
        assert done["withdrawn"] is True and done["approval_ids"] == [approval_id]


@pytest.mark.parametrize("text,expected", [
    ("确认撤回", True), ("确认撤回 SO20261008002", True), ("好的，确认撤回", True), ("确定撤回退款申请", True),
    ("confirm withdrawal", True), ("确认撤回吗？", False), ("确认撤回？还是再想想", False), ("撤回", True),
    ("是的，撤回", True), ("确认撤回这笔退款", True), ("确认撤回订单SO20261008002的退款申请", True),
    ("确认退款", False), ("确认撤回 然后重新申请", False),
])
def test_withdraw_confirmed(text, expected):
    from app.textutil import withdraw_confirmed

    assert withdraw_confirmed(text) is expected


def test_an_offer_ends_when_the_run_went_on_or_a_customer_turn_went_unanswered(client, ask, login):
    """Review: a "好的" is a yes only to the offer itself, not to a later question in the same run or after
    a message nobody answered."""
    from app.agent.business_tools import _withdraw_offer
    from app.agent.tools import ToolContext
    from app.models import Message, Tenant, User
    from app.services import get_services

    customer, approval_id = _file(ask, login)
    conversation, _ = ask(customer, f"{ORDER} 撤回退款申请")
    services = get_services()
    with new_session() as db:
        user = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
        ctx = ToolContext(db=db, settings=services.settings, tenant=db.get(Tenant, user.tenant_id), user=user,
                          embedder=services.embedder, run_id="t", conversation_id=conversation, customer_text="好的")
        assert _withdraw_offer(ctx) is not None
        last = db.execute(select(Message).where(Message.conversation_id == conversation)
                          .order_by(Message.id.desc()).limit(1)).scalar_one()
        run = db.get(Run, last.run_id)
        run.state = {**run.state, "tools": [*run.state["tools"], "track_shipment"]}
        db.flush()
        assert _withdraw_offer(ctx) is None, "the run went on to something else"
        run.state = {**run.state, "tools": run.state["tools"][:-1]}
        db.add_all([Message(tenant_id=user.tenant_id, conversation_id=conversation, role="user", content=c)
                    for c in ("等等 我再想想 不撤了", "好的")])
        db.flush()
        assert _withdraw_offer(ctx) is None, "a customer turn went unanswered in between"
        db.rollback()
