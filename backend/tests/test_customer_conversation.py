"""A customer's conversation: who may write in it, and what stays of what they wrote.

- Staff could post into any customer's conversation (/api/chat, the stream, the voice channel). It was stored and
  answered as the customer's words, so Mia could write "算了 不用人工了" and close the complaint about herself; the
  transcript said the customer cancelled.
- "退款多久到賬" came back in the customer's own transcript as "退款多久到账": the folded copy was what was stored.
"""

import pytest
from sqlalchemy import select

from app.api.voice import _authorize
from app.db import new_session
from app.models import Feedback, Run, Tenant, Ticket
from tests.conftest import by_type

COMPLAINT = "我要投诉你们的客服Mia 态度太差了"


def _messages(client, headers, conversation: int) -> list[dict]:
    return client.get(f"/api/conversations/{conversation}/messages", headers=headers).json()["messages"]


def _post(client, way: str, headers: dict, conversation: int, text: str):
    if way == "chat":
        return client.post("/api/chat", json={"content": text, "conversation_id": conversation}, headers=headers)
    return client.post(f"/api/conversations/{conversation}/stream", json={"content": text}, headers=headers)


@pytest.mark.parametrize("way", ["chat", "stream"])
@pytest.mark.parametrize("who", ["agent@leon.demo", "admin@leon.demo"])
def test_staff_cannot_write_as_the_customer(client, ask, login, way, who):
    jack, staff = login("customer2@leon.demo"), login(who)
    conversation, _ = ask(jack, COMPLAINT)
    before = _messages(client, staff, conversation)
    response = _post(client, way, staff, conversation, "算了 不用人工了")
    assert response.status_code == 403 and "转人工队列" in response.json()["detail"]
    assert _messages(client, staff, conversation) == before, "they still read it; nothing they sent is in it"
    with new_session() as db:
        ticket = db.execute(select(Ticket).where(Ticket.conversation_id == conversation)).scalar_one()
    assert ticket.status == "open" and ticket.resolution in (None, ""), "the complaint is not closed as the customer's"


def test_the_customer_still_writes_and_staff_still_have_their_own_conversations(client, ask, login):
    jack, mia = login("customer2@leon.demo"), login("agent@leon.demo")
    conversation, _ = ask(jack, "转人工")
    assert _post(client, "chat", jack, conversation, "算了 不用人工了").status_code == 200
    own, _ = ask(mia, "退款多久到账")
    assert _post(client, "chat", mia, own, "积分怎么用").status_code == 200


def test_another_customer_still_gets_not_found(client, ask, login):
    conversation, _ = ask(login("customer2@leon.demo"), "转人工")
    assert _post(client, "chat", login("customer@leon.demo"), conversation, "你好").status_code == 404


def test_the_voice_channel_is_the_owners_too(ask, login):
    jack, mia = login("customer2@leon.demo"), login("agent@leon.demo")
    conversation, _ = ask(jack, "转人工")
    token = lambda headers: headers["Authorization"].split()[1]  # noqa: E731
    principal, _ = _authorize({"token": token(jack), "conversation_id": conversation})
    assert principal.user.email == "customer2@leon.demo"
    with pytest.raises(LookupError):
        _authorize({"token": token(mia), "conversation_id": conversation})


# ---------------------------------------------------------------- what they wrote
def test_the_transcript_keeps_what_the_customer_wrote(client, ask, login):
    emma = login("customer@leon.demo")
    conversation, events = ask(emma, "退款多久到賬")
    assert "1-3个工作日到账" in by_type(events, "final")[0]["text"], "read in simplified"
    first = _messages(client, emma, conversation)[0]
    assert first["role"] == "user" and first["content"] == "退款多久到賬"
    title = next(c["title"] for c in client.get("/api/conversations", headers=emma).json() if c["id"] == conversation)
    assert title == "退款多久到賬"
    with new_session() as db:
        assert db.execute(select(Run.input_text).where(Run.conversation_id == conversation)).scalar_one() == "退款多久到账"


def test_a_traditional_shop_keeps_either_script(client, ask, login):
    with new_session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == "iris")).scalar_one()
        tenant.locale = "zh-Hant-HK"
        db.commit()
    noah = login("customer@iris.demo")
    for text in ("免費版有多少存儲空間", "免费版有多少存储空间"):
        conversation, _ = ask(noah, text)
        assert _messages(client, noah, conversation)[0]["content"] == text


def test_the_next_turn_reads_the_earlier_one_folded(ask, login, client):
    emma = login("customer@leon.demo")
    conversation, _ = ask(emma, "SO20261008002 的訂單到哪了")
    _, events = ask(emma, "那退款多久到賬", conversation_id=conversation)
    assert "1-3个工作日到账" in by_type(events, "final")[0]["text"]
    assert _messages(client, emma, conversation)[0]["content"] == "SO20261008002 的訂單到哪了"


def test_personal_data_is_masked_however_it_was_written(client, ask, login):
    emma = login("customer@leon.demo")
    conversation, _ = ask(emma, "我的電話是１３８００１３８０００ 退款多久到賬")
    stored = _messages(client, emma, conversation)[0]["content"]
    assert "138****8000" in stored and "電話" in stored and "13800138000" not in stored and "１３８" not in stored


def test_the_ticket_quotes_their_words_and_finds_the_intent_in_the_folded_text(client, ask, login):
    jack, leon = login("customer2@leon.demo"), login("admin@leon.demo")
    conversation, _ = ask(jack, "我要投訴 轉人工")
    card = next(h for h in client.get("/api/handoffs", headers=leon).json() if h["conversation_id"] == conversation)
    assert "顾客原话：我要投訴 轉人工" in card["reason"] and "诉求：投诉" in card["reason"]
    assert card["last_message"] == "我要投訴 轉人工"


def test_the_quote_is_only_the_turn_being_handed_over(client, ask, login):  # second-model review P3
    jack, leon = login("customer2@leon.demo"), login("admin@leon.demo")
    conversation, _ = ask(jack, "转人工")
    ask(jack, "算了 不用人工了 我要投诉客服Mia 态度太差", conversation_id=conversation)
    card = next(h for h in client.get("/api/handoffs", headers=leon).json() if h["conversation_id"] == conversation)
    assert "顾客原话：我要投诉客服Mia" in card["reason"] and "算了" not in card["reason"], card["reason"]


def test_feedback_keeps_the_form_the_rules_read(client, ask, login):
    emma = login("customer@leon.demo")
    conversation, events = ask(emma, "退款多久到賬")
    message_id = by_type(events, "final")[0]["message_id"]
    assert client.post(f"/api/messages/{message_id}/feedback", json={"rating": "down"}, headers=emma).status_code == 200
    with new_session() as db:
        assert db.execute(select(Feedback.question).where(Feedback.message_id == message_id)).scalar_one() == "退款多久到账"
