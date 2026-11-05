"""N-222: a complaint that names a member of staff was theirs to handle.

Jack wrote "我要投诉你们的客服Mia 态度太差了"; the handoff said only "投诉", and Mia could reply to it and close it,
handing the conversation back to the bot. The ticket now names her, she cannot act on it, and a complaint about an
agent needs an administrator; the audit records who handled a complaint about whom.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

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


# ---------------------------------------------------------------- a later correction (first name stuck)
def _with_noah() -> None:
    with new_session() as db:
        leon = db.execute(select(Tenant).where(Tenant.slug == "leon")).scalar_one()
        db.add(User(tenant_id=leon.id, email="noah@leon.demo", name="Noah", role="agent",
                    password_hash=hash_password(DEMO_PASSWORD)))
        db.commit()


@pytest.mark.parametrize("text", [
    "不对 我要投诉的是Noah", "不是Mia 是Noah", "说错了 是Noah", "我不是要投诉Mia 我要投诉Noah", "不对 Noah", "投诉的是客服Noah",
    "搞错了 是客服Noah态度差", "不是Mia态度差 是Noah态度差", "是Noah不是Mia", "Noah才是",
    # second-model re-review: more after the name, or the denial said another way
    "不是Mia 是Noah的问题", "不是Mia 是Noah哦", "不是Mia 是Noah呢", "Mia没错 是Noah", "名字写错了 是Noah", "我要投诉的人是Noah", "不对 是Noah那个客服"])
def test_a_correction_moves_the_rules_to_the_person_it_names(client, ask, login, text):
    _with_noah()
    jack, mia, noah, leon = login("customer2@leon.demo"), login("agent@leon.demo"), login("noah@leon.demo"), login("admin@leon.demo")
    conversation, _ = ask(jack, COMPLAINT)
    ask(jack, text, conversation_id=conversation)
    card = _handoff(client, leon, conversation)
    assert card["subject_staff"] == "Noah", text
    assert "涉及员工：Noah" in card["reason"] and "涉及员工：Mia" not in card["reason"] and "顾客更正：投诉对象不是 Mia" in card["reason"]
    ticket = card["ticket_id"]
    # the person it names now is refused, and a complaint about an agent stays an administrator's
    assert client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好"}, headers=noah).status_code == 403
    assert _handoff(client, noah, conversation)["blocked"] == "这条投诉涉及您本人，请由管理员处理"
    assert client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好"}, headers=mia).status_code == 403
    assert _handoff(client, mia, conversation)["blocked"] == "涉及员工的投诉需要管理员处理"
    assert client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好"}, headers=leon).status_code == 200
    with new_session() as db:
        ids = dict(db.execute(select(User.name, User.id).where(User.name.in_(["Mia", "Noah", "Jack"]))).all())
        moved = db.execute(select(AuditLog).where(AuditLog.action == "handoff.subject", AuditLog.target == f"handoff:{ticket}")).scalar_one()
    assert moved.actor_id == ids["Jack"]
    assert moved.detail["from_staff_id"] == ids["Mia"] and moved.detail["subject_staff_id"] == ids["Noah"]


@pytest.mark.parametrize("text", ["搞错了 不是Mia", "投诉的不是Mia", "我不是要投诉Mia 我要投诉快递", "Mia没错", "不关Mia的事",
                                  "应该不是Mia", "刚才说的不是Mia", "那个人不是Mia", "客服不是Mia", "名字不是Mia", "我记错了不是Mia",
                                  "之前接待我的不是Mia", "态度差的不是Mia"])
def test_a_correction_that_names_nobody_lifts_the_block(client, ask, login, text):
    jack, mia = login("customer2@leon.demo"), login("agent@leon.demo")
    conversation, _ = ask(jack, COMPLAINT)
    ask(jack, text, conversation_id=conversation)
    card = _handoff(client, mia, conversation)
    assert card["subject_staff"] is None and card["blocked"] is None, text
    assert "涉及员工" not in card["reason"] and "顾客更正：投诉对象不是 Mia" in card["reason"]
    assert client.post(f"/api/handoffs/{card['ticket_id']}/reply", json={"content": "您好"}, headers=mia).status_code == 200


@pytest.mark.parametrize("text", [
    "不对 我的订单号是SO20261015009", "不是Mia吗", "不对 Noah说可以退的", "不是Mia的话我就不投诉了", "你们搞错了我的订单", "不对 我要投诉的是Mia",
    "不是 我要退款", "我不投诉了",
    # second-model review: a question, or a sentence about something else, is not a correction
    "是不是Mia在处理", "不是Mia接待的我吗", "是不是Mia搞错了", "退款不是Mia批的吗", "不是Mia负责吗", "退款不是Mia批的",
    "说错了 是Noah发的货", "不对 是Noah接待的", "不是 是Noah给我换的货", "我记错了 是Noah接待的"])
def test_other_messages_leave_the_complaint_where_it_is(client, ask, login, text):
    _with_noah()
    jack, leon = login("customer2@leon.demo"), login("admin@leon.demo")
    conversation, _ = ask(jack, COMPLAINT)
    ask(jack, text, conversation_id=conversation)
    card = _handoff(client, leon, conversation)
    assert card["subject_staff"] == "Mia" and "涉及员工：Mia" in card["reason"] and "更正" not in card["reason"], text


@pytest.mark.parametrize("text", ["不对 应该是Noah", "我记错了 是Noah接待的", "说错了 是Noah发的货", "不对 之前是Noah处理的", "不是 是Noah给我换的货"])
def test_a_correction_with_no_complaint_behind_it_names_nobody(client, ask, login, text):  # second-model review P1
    _with_noah()
    jack, mia = login("customer2@leon.demo"), login("agent@leon.demo")
    conversation, _ = ask(jack, "转人工")
    ticket = _handoff(client, mia, conversation)["ticket_id"]
    client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好，我是 Mia"}, headers=mia)
    ask(jack, text, conversation_id=conversation)
    card = _handoff(client, mia, conversation)
    assert card["subject_staff"] is None and card["assignee"] == "Mia" and card["status"] == "in_progress", (text, card)
    assert client.post(f"/api/handoffs/{ticket}/reply", json={"content": "好的"}, headers=mia).status_code == 200


def test_a_first_turn_with_no_complaint_names_nobody(client, ask, login):
    _with_noah()
    conversation, _ = ask(login("customer2@leon.demo"), "转人工 不对 是Noah接待的我")
    assert _handoff(client, login("admin@leon.demo"), conversation)["subject_staff"] is None


def test_another_complaint_without_a_correction_is_noted_not_moved(client, ask, login):
    _with_noah()
    jack, leon = login("customer2@leon.demo"), login("admin@leon.demo")
    conversation, _ = ask(jack, COMPLAINT)
    for _ in range(2):
        ask(jack, "我还要投诉Noah", conversation_id=conversation)
    card = _handoff(client, leon, conversation)
    assert card["subject_staff"] == "Mia" and card["reason"].count("顾客还提到：Noah") == 1


@pytest.mark.parametrize("text,subject", [
    ("我要投诉Mia 不对 是Noah", "Noah"), ("我要投诉Noah，说错了，我要投诉的是Mia", "Mia"), ("我要投诉Mia 不对 不是Mia", None),
    ("我要投诉Mia不对是Noah", "Noah"), ("我要投诉Noah 不是Mia", "Noah")])
def test_a_correction_in_the_same_message_counts(client, ask, login, text, subject):
    _with_noah()
    conversation, _ = ask(login("customer2@leon.demo"), text)
    assert _handoff(client, login("admin@leon.demo"), conversation)["subject_staff"] == subject, text


def test_a_correction_to_the_person_holding_it_returns_it_to_the_queue(client, ask, login):
    jack, mia, leon = login("customer2@leon.demo"), login("agent@leon.demo"), login("admin@leon.demo")
    conversation, _ = ask(jack, COMPLAINT)
    ticket = _handoff(client, leon, conversation)["ticket_id"]
    client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好，我是管理员 Leon"}, headers=leon)
    assert _handoff(client, leon, conversation)["assignee"] == "Leon"
    _, events = ask(jack, "不对 我要投诉的是管理员Leon", conversation_id=conversation)
    card = _handoff(client, leon, conversation)
    assert card["subject_staff"] == "Leon" and card["assignee"] is None and card["status"] == "open", card
    assert "排队" in by_type(events, "final")[0]["text"]
    assert client.post(f"/api/handoffs/{ticket}/reply", json={"content": "抱歉"}, headers=leon).status_code == 403
    # a complaint about the administrator is for the others: Mia, named by mistake, may take it now
    assert _handoff(client, mia, conversation)["blocked"] is None
    assert client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好，我是 Mia"}, headers=mia).status_code == 200


def test_you_in_a_correction_is_the_person_holding_it(client, ask, login):
    jack, leon = login("customer2@leon.demo"), login("admin@leon.demo")
    conversation, _ = ask(jack, COMPLAINT)
    ticket = _handoff(client, leon, conversation)["ticket_id"]
    client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好"}, headers=leon)
    ask(jack, "不对 我要投诉的是你", conversation_id=conversation)
    card = _handoff(client, leon, conversation)
    assert card["subject_staff"] == "Leon" and card["assignee"] is None and card["status"] == "open", card


@pytest.mark.parametrize("first,correction", [
    ("我要投诉管理员Leon 态度太差了", "不对 我要投诉的是Mia"),  # admin -> agent: a correction of the name
    (None, "我要投诉Mia"),                                  # nobody -> agent: a first complaint
])
def test_an_agent_holding_it_goes_when_it_becomes_an_administrators(client, ask, login, first, correction):
    _with_noah()
    jack, noah, leon = login("customer2@leon.demo"), login("noah@leon.demo"), login("admin@leon.demo")
    conversation, _ = ask(jack, first or "转人工")
    ticket = _handoff(client, noah, conversation)["ticket_id"]
    assert client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好，我是 Noah"}, headers=noah).status_code == 200
    ask(jack, correction, conversation_id=conversation)
    card = _handoff(client, leon, conversation)
    assert card["subject_staff"] == "Mia" and card["assignee"] is None and card["status"] == "open", card
    assert client.post(f"/api/handoffs/{ticket}/reply", json={"content": "您好"}, headers=noah).status_code == 403


def test_a_correction_to_someone_nobody_else_can_replace_lifts_the_block_and_says_so(client, ask, login):
    jack, leon = login("customer2@leon.demo"), login("admin@leon.demo")
    conversation, _ = ask(jack, COMPLAINT)
    with new_session() as db:  # Leon is the only member of staff left
        db.execute(update(User).where(User.email == "agent@leon.demo").values(disabled_at=datetime.now(timezone.utc)))
        db.commit()
    ask(jack, "不对 我要投诉的是管理员Leon", conversation_id=conversation)
    card = _handoff(client, leon, conversation)
    assert card["subject_staff"] is None and card["blocked"] is None
    assert "而是 Leon（没有其他同事可以处理）" in card["reason"]
