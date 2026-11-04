"""N-222: a complaint that names a member of staff was theirs to handle.

Jack wrote "我要投诉你们的客服Mia 态度太差了"; the handoff said only "投诉", and Mia could reply to it and close it,
handing the conversation back to the bot. The ticket now names her, she cannot act on it, and a complaint about an
agent needs an administrator; the audit records who handled a complaint about whom.
"""

import pytest
from sqlalchemy import select

from app.db import new_session
from app.models import AuditLog, Tenant, Ticket, User
from app.seed import DEMO_PASSWORD
from app.security import hash_password
from tests.conftest import by_type

COMPLAINT = "我要投诉你们的客服Mia 态度太差了"


def _handoff(client, headers, conversation_id: int) -> dict:
    return next(h for h in client.get("/api/handoffs", headers=headers).json() if h["conversation_id"] == conversation_id)


def test_the_agent_complained_about_cannot_reply_or_close(client, ask, login):
    conversation, _ = ask(login("customer2@leon.demo"), COMPLAINT)
    mia, leon = login("agent@leon.demo"), login("admin@leon.demo")
    card = _handoff(client, mia, conversation)
    assert card["subject_staff"] == "Mia" and card["blocked"] == "这条投诉涉及您本人，请由管理员处理"
    assert "涉及员工：Mia" in card["reason"]
    ticket = card["ticket_id"]
    reply = client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好，我是Mia"}, headers=mia)
    assert reply.status_code == 403 and reply.json()["detail"] == card["blocked"]
    assert client.post(f"/api/handoffs/{ticket}/close", headers=mia).status_code == 403
    assert _handoff(client, mia, conversation)["status"] == "open", "still waiting: nothing Mia did took effect"
    # An administrator handles it, and the audit says whose complaint it was.
    assert _handoff(client, leon, conversation)["blocked"] is None
    assert client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好，我是管理员Leon"}, headers=leon).status_code == 200
    assert client.post(f"/api/handoffs/{ticket}/close", headers=leon).status_code == 200
    with new_session() as db:
        mia_id = db.execute(select(User.id).where(User.email == "agent@leon.demo")).scalar_one()
        actions = db.execute(select(AuditLog.action, AuditLog.detail).where(AuditLog.target == f"handoff:{ticket}")).all()
    assert {a for a, _ in actions} == {"handoff.reply", "handoff.close"}
    assert all(d["subject_staff_id"] == mia_id for _, d in actions)


def test_another_agent_cannot_take_a_complaint_about_an_agent_either(client, ask, login):
    with new_session() as db:
        leon = db.execute(select(Tenant).where(Tenant.slug == "leon")).scalar_one()
        db.add(User(tenant_id=leon.id, email="noah@leon.demo", name="Noah", role="agent",
                    password_hash=hash_password(DEMO_PASSWORD)))
        db.commit()
    conversation, _ = ask(login("customer2@leon.demo"), COMPLAINT)
    other = login("noah@leon.demo")
    card = _handoff(client, other, conversation)
    assert card["blocked"] == "涉及员工的投诉需要管理员处理"
    assert client.post(f"/api/handoffs/{card['ticket_id']}/reply", json={"content": "您好"}, headers=other).status_code == 403


def test_earlier_agent_in_the_conversation_is_the_subject(client, ask, login):
    jack, mia = login("customer2@leon.demo"), login("agent@leon.demo")
    conversation, _ = ask(jack, "转人工")
    ticket = _handoff(client, mia, conversation)["ticket_id"]
    client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好，我是 Mia"}, headers=mia)
    client.post(f"/api/handoffs/{ticket}/close", headers=mia)
    ask(jack, "刚才那个客服太敷衍了 我要投诉", conversation_id=conversation)
    assert _handoff(client, mia, conversation)["subject_staff"] == "Mia"


def test_a_complaint_while_she_holds_the_handoff_takes_it_from_her(client, ask, login):  # second-model review P1
    jack, mia, leon = login("customer2@leon.demo"), login("agent@leon.demo"), login("admin@leon.demo")
    conversation, _ = ask(jack, "转人工")
    ticket = _handoff(client, mia, conversation)["ticket_id"]
    client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好，我是 Mia"}, headers=mia)
    _, events = ask(jack, "我要投诉你 Mia 态度太差了", conversation_id=conversation)
    card = _handoff(client, mia, conversation)
    assert card["subject_staff"] == "Mia" and card["assignee"] is None and card["status"] == "open", card
    assert "排队" in by_type(events, "final")[0]["text"], "back in the queue, not '人工客服 Mia 正在为您服务'"
    assert client.post(f"/api/handoffs/{ticket}/reply", json={"content": "抱歉"}, headers=mia).status_code == 403
    assert client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好，我是管理员 Leon"}, headers=leon).status_code == 200


@pytest.mark.parametrize("text,subject", [
    ("你态度太差了 我要投诉", "Mia"), ("刚才那个客服太敷衍了 我要投诉", "Mia"),
    ("Mia 我要投诉物流太慢", None), ("谢谢Mia 不过我要投诉快递", None), ("我要投诉你们 Mia帮我催一下", None), ("投诉", None)])
def test_a_complaint_during_a_live_handoff_names_only_who_it_is_about(client, ask, login, text, subject):  # re-review P2
    jack, mia = login("customer2@leon.demo"), login("agent@leon.demo")
    conversation, _ = ask(jack, "转人工")
    ticket = _handoff(client, mia, conversation)["ticket_id"]
    client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好，我是 Mia"}, headers=mia)
    ask(jack, text, conversation_id=conversation)
    card = _handoff(client, mia, conversation)
    assert card["subject_staff"] == subject, text
    assert (card["assignee"] is None) is (subject is not None), "only a complaint about her takes it from her"


@pytest.mark.parametrize("text", ["我要投诉你们的客服 Mia，态度太差", "投诉 mia", "投诉leon优选的客服mia", "客服 Mia 态度差 我要投诉"])
def test_spacing_and_the_shop_name_do_not_hide_the_name(client, ask, login, text):  # re-review P2
    conversation, _ = ask(login("customer2@leon.demo"), text)
    assert _handoff(client, login("agent@leon.demo"), conversation)["subject_staff"] == "Mia", text


def test_the_only_administrator_is_not_locked_out_of_a_complaint_about_them(client, ask, login):  # review P1
    iris = login("admin@iris.demo")
    for text in ("我要投诉Iris云文档 老是崩", "我要投诉iris云文档 老是崩", "我要投诉客服Iris 态度太差"):
        conversation, _ = ask(login("customer@iris.demo"), text)
        card = _handoff(client, iris, conversation)
        assert card["blocked"] is None, text
        assert client.post(f"/api/handoffs/{card['ticket_id']}/close", headers=iris).status_code == 200


def test_the_shop_or_no_named_person_is_not_a_staff_complaint(client, ask, login):
    mia = login("agent@leon.demo")
    for text in ("我要投诉Leon优选 发货太慢", "我要投诉 快递太慢了", "我不是要投诉Mia，转人工",
                 # second-model review P2: the shop in any case or spacing, or Mia named but not complained about
                 "我要投诉leon优选 发货太慢", "我要投诉Leon 优选 发货太慢", "我要投诉你们Leon家的快递",
                 "Mia帮我处理的退款还没到 我要投诉物流", "客服Mia态度很好 我不投诉了", "我不是要投诉Mia 我要投诉快递",
                 "Mia态度很好 但是快递员态度太差了 我要投诉", "投诉流程怎么走 Mia说可以在这里投诉"):
        conversation, _ = ask(login("customer2@leon.demo"), text)
        card = _handoff(client, mia, conversation)
        assert card["subject_staff"] is None and card["blocked"] is None, text
    with new_session() as db:
        assert not db.execute(select(Ticket).where(Ticket.subject_staff_id.is_not(None))).scalars().first()
