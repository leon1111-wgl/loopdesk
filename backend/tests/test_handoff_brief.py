"""N-058: a handoff ticket says what the customer wants, so the person who picks it up need not ask again."""

from sqlalchemy import select

from app.agent.handoff import QUOTE_LIMIT, brief
from app.db import new_session
from app.models import Ticket
from tests.conftest import by_type


def _ticket(conversation_id):
    with new_session() as db:
        return db.execute(select(Ticket).where(Ticket.conversation_id == conversation_id,
                                               Ticket.kind == "handoff")).scalar_one()


def test_the_ticket_keeps_the_order_the_intents_and_the_customer_words(client, ask, login):
    # The nitpick reproduction: before, title and description were both "顾客要求转人工".
    emma, mia = login("customer@leon.demo"), login("agent@leon.demo")
    conversation_id, events = ask(emma, "SO20261008010 杯子碎了！！！我要退款，还要投诉你们快递")
    assert by_type(events, "final")[0]["status"] == "handoff"
    ticket = _ticket(conversation_id)
    assert ticket.title == "转人工：退款、投诉、商品问题、物流 · SO20261008010"
    assert ticket.description.splitlines() == [
        "原因：顾客要求转人工", "订单：SO20261008010", "诉求：退款、投诉、商品问题、物流",
        "顾客原话：SO20261008010 杯子碎了！！！我要退款，还要投诉你们快递",
    ]

    # A follow-up while waiting replaces the last message, not what the ticket is about.
    ask(emma, "快点！！", conversation_id=conversation_id)
    card = next(h for h in client.get("/api/handoffs", headers=mia).json() if h["conversation_id"] == conversation_id)
    assert card["summary"] == "退款、投诉、商品问题、物流 · SO20261008010"
    assert card["last_message"] == "快点！！"
    assert "顾客原话：SO20261008010 杯子碎了" in card["reason"]


def test_the_quote_is_masked_and_cut(ask, login):
    conversation_id, _ = ask(login("customer@leon.demo"), "我要投诉，电话 13812345678 " + "一直没人管" * 40)
    description = _ticket(conversation_id).description
    assert "13812345678" not in description, "staff read the masked text, like the trace"
    quote = description.split("顾客原话：", 1)[1]
    assert len(quote) == QUOTE_LIMIT + 1 and quote.endswith("…")


def test_a_negated_complaint_is_not_labelled_one(ask, login):
    conversation_id, _ = ask(login("customer@leon.demo"), "不是要投诉，转人工")
    ticket = _ticket(conversation_id)
    assert "投诉" not in ticket.title and "诉求" not in ticket.description
    assert ticket.title == "转人工：顾客要求转人工", "a bare request reads as it did before"


def test_a_handoff_the_agent_falls_back_to_keeps_the_question(settings, ask, login):
    # Not only the "转人工" rule: when the agent gives up (here, at the step limit) the ticket
    # still carries the order the customer asked about.
    settings.max_agent_steps = 1
    conversation_id, events = ask(login("customer@leon.demo"), "SO20261008002 到哪了")
    assert by_type(events, "final")[0]["status"] == "handoff"
    ticket = _ticket(conversation_id)
    assert ticket.title == "转人工：Agent 达到最大步数 · SO20261008002", "no intent words: the reason heads the title"
    assert ticket.description.splitlines() == [
        "原因：Agent 达到最大步数", "订单：SO20261008002", "顾客原话：SO20261008002 到哪了"]


def test_brief_dedupes_orders_and_normalises_width():
    title, description = brief("顾客要求转人工", "ｓｏ２０２６１００８００２ 和 SO20261008002 都没收到，还有 SO20261015009")
    assert title == "转人工：物流 · SO20261008002"
    assert "订单：SO20261008002、SO20261015009" in description
    assert brief("顾客要求转人工", "")[1] == "原因：顾客要求转人工"


def test_a_follow_up_handoff_keeps_the_order_from_the_earlier_turn(ask, login):
    # Review: "太慢了，转人工" after "SO… 到哪了" is about that order; the quote is still this turn.
    emma = login("customer@leon.demo")
    conversation_id, _ = ask(emma, "SO20261008002 到哪了")
    _, events = ask(emma, "太慢了，转人工", conversation_id=conversation_id)
    assert by_type(events, "final")[0]["status"] == "handoff"
    ticket = _ticket(conversation_id)
    assert ticket.title == "转人工：顾客要求转人工 · SO20261008002"
    assert ticket.description.splitlines() == ["原因：顾客要求转人工", "订单：SO20261008002", "顾客原话：太慢了，转人工"]


def test_the_tool_called_by_the_model_quotes_the_turn(settings, ask, login):
    from app.agent.business_tools import handoff_to_human
    from app.agent.tools import ToolContext
    from app.models import Tenant, User

    with new_session() as db:
        jack = db.execute(select(User).where(User.email == "customer2@leon.demo")).scalar_one()
        ctx = ToolContext(db=db, settings=settings, tenant=db.get(Tenant, jack.tenant_id), user=jack,
                          embedder=None, run_id="test", conversation_id=None,
                          customer_text="SO20261015009 换货换了两次还是坏的")
        assert handoff_to_human(ctx, {"reason": "顾客多次换货仍有问题"})["queued"] is True
        ticket = db.execute(select(Ticket).where(Ticket.kind == "handoff", Ticket.customer_id == jack.id)).scalar_one()
        assert ticket.title == "转人工：换货、商品问题 · SO20261015009"
        assert ticket.description.startswith("原因：顾客多次换货仍有问题\n订单：SO20261015009")


def test_labels_follow_what_the_customer_means():
    # Review: refund money "没到账" is not a parcel, and a negated intent is not one.
    def intents(text):
        return brief("r", text)[0].removeprefix("转人工：")

    assert intents("退款还没到账") == "退款"
    assert intents("我没收到退款") == "退款"
    assert intents("快递还没到") == "物流"
    assert intents("我不是要退款，是要换货") == "换货"
    assert intents("别给我退款") == "r"
    assert intents("不想投诉，就想问问什么时候发货") == "物流"
    assert intents("我不需要发票了") == "r"
    assert intents("换一个收货地址") == "r"
    assert intents("质量很好但是尺寸不对") == "r"


def test_a_long_reason_still_leaves_room_for_the_order():
    title, _ = brief("模型给的很长的原因" * 30, "SO20261008002")
    assert len(title) <= 60 and title.endswith(" · SO20261008002")


def test_an_order_from_a_finished_handoff_is_not_carried_over(client, ask, login):
    emma, mia = login("customer@leon.demo"), login("agent@leon.demo")
    conversation_id, _ = ask(emma, "SO20261008002 坏了，转人工")
    first = _ticket(conversation_id).id
    assert client.post(f"/api/handoffs/{first}/close", headers=mia).status_code == 200
    ask(emma, "转人工", conversation_id=conversation_id)
    with new_session() as db:
        second = db.execute(select(Ticket).where(Ticket.conversation_id == conversation_id, Ticket.id != first)).scalar_one()
    assert second.title == "转人工：顾客要求转人工" and "订单" not in second.description
