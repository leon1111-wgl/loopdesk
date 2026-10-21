"""Personal data typed into any write endpoint is masked before it is stored (N-083).

Customer messages were masked by the agent runtime, but a feedback comment (the box a customer
fills in after clicking "down", the likeliest place to leave a phone number), a correction, a
conversation title and the notes staff write went to the database as typed, and came back to every
agent through GET /api/flywheel/feedback. Rather than one test per endpoint, the main test writes
the same personal data through every free-text endpoint and then reads every text column of every
table: an endpoint added later without masking fails it.
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import JSON, String, Text, select

from app.db import Base, get_engine, new_session
from app.flywheel.service import key_facts
from app.models import Feedback, Refund, Tenant, User
from tests.conftest import by_type
from tests.test_conflicts import CONFLICTING
from tests.test_providers_and_channels import _feishu_event
from tests.test_tickets import BROKEN

PHONE, ID_CARD, CARD = "13987654321", "320102198807154321", "6217001234567812345"
PII = f"打我电话{PHONE}，身份证{ID_CARD}，卡号{CARD}"


def _assistant_message(client, headers, conversation_id: int) -> int:
    page = client.get(f"/api/conversations/{conversation_id}/messages", headers=headers).json()
    return [m["id"] for m in page["messages"] if m["role"] == "assistant"][-1]


def _stored_text() -> str:
    """Every text and JSON column of every table, as one string."""
    parts = []
    with get_engine().connect() as conn:
        for table in Base.metadata.sorted_tables:
            columns = [c for c in table.columns if isinstance(c.type, (String, Text, JSON))]
            if columns:
                parts += [str(tuple(row)) for row in conn.execute(select(*columns))]
    return "\n".join(parts)


def _write_pii_everywhere(client, ask, login) -> None:
    emma, mia, admin = login("customer@leon.demo"), login("agent@leon.demo"), login("admin@leon.demo")
    ok = lambda r: r.status_code == 200 or (_ for _ in ()).throw(AssertionError(r.text))  # noqa: E731

    # The customer: a conversation title, a message, a feedback comment.
    titled = client.post("/api/conversations", json={"title": f"退款 {PHONE}"}, headers=emma).json()["id"]
    ask(emma, f"退款多久到账？{PII}", conversation_id=titled)
    message_id = _assistant_message(client, emma, titled)
    ok(client.post(f"/api/messages/{message_id}/feedback", json={"rating": "down", "comment": PII}, headers=emma))
    # Staff: a correction, an approval comment, a handoff reply, a ticket resolution.
    ok(client.post(f"/api/messages/{message_id}/feedback",
                   json={"rating": "correction", "correction": f"退款1-3个工作日到账，回访电话 {PHONE}"}, headers=mia))
    _, events = ask(emma, "SO20261008002 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    ok(client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "reject", "comment": PII}, headers=admin))
    ask(emma, "转人工")
    handoff_id = client.get("/api/handoffs", headers=mia).json()[0]["ticket_id"]
    ok(client.post(f"/api/handoffs/{handoff_id}/reply", json={"content": f"已登记，{PII}"}, headers=mia))
    ask(login("customer2@leon.demo"), BROKEN)
    [ticket] = client.get("/api/tickets", headers=mia).json()  # issue tickets
    ticket_id = ticket["id"]
    ok(client.post(f"/api/tickets/{ticket_id}/close", json={"resolution": f"已回电 {PII}"}, headers=mia))
    # Administrators: a refund settled by hand, a knowledge conflict resolved.
    with new_session() as db:
        user = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
        refund = Refund(tenant_id=user.tenant_id, order_id="SO-PII-1", customer_id=user.id, amount=59.0,
                        status="pending", idempotency_key=uuid.uuid4().hex,
                        created_at=datetime.now(timezone.utc) - timedelta(hours=30))
        db.add(refund)
        db.commit()
        refund_id = refund.id
    ok(client.post(f"/api/refunds/{refund_id}/settle", headers=admin,
                   json={"outcome": "succeeded", "note": f"顾客来电 {PII}"}))
    client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin)
    conflict_id = client.get("/api/kb/conflicts", headers=admin).json()[0]["id"]
    ok(client.post(f"/api/kb/conflicts/{conflict_id}/resolve", json={"note": f"和顾客 {PHONE} 确认过"}, headers=admin))
    # A Feishu customer: the event row outlives the run (demo mode accepts unsigned events).
    ok(client.post("/api/channels/feishu/leon/events", json=_feishu_event("evt-pii", f"退款多久到账 {PII}")))


def test_no_free_text_endpoint_stores_personal_data_as_typed(client, ask, login):
    _write_pii_everywhere(client, ask, login)
    stored = _stored_text()
    for value in (PHONE, ID_CARD, CARD):
        assert value not in stored, f"{value} stored unmasked"
    assert "139****4321" in stored, "masked, not dropped: staff still see which number it was"


def test_staff_text_keeps_the_tenants_own_contacts(client, ask, login):
    # Review: a reply telling the customer where to write or whom to call must still say it.
    with new_session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == "leon")).scalar_one()
        tenant.official_contacts = ["13800138000"]
        db.commit()
    emma, mia = login("customer@leon.demo"), login("agent@leon.demo")
    conversation_id, _ = ask(emma, "转人工")
    handoff_id = client.get("/api/handoffs", headers=mia).json()[0]["ticket_id"]
    reply = ("请寄到 service@leonshop.example 或致电 138-0013-8000；"
             f"不要发到 leon.refund@gmail.com，也不要联系 {PHONE}")
    r = client.post(f"/api/handoffs/{handoff_id}/reply", json={"content": reply}, headers=mia)
    assert r.status_code == 200, r.text
    page = client.get(f"/api/conversations/{conversation_id}/messages", headers=emma).json()
    assert page["messages"][-1]["content"] == ("请寄到 service@leonshop.example 或致电 138-0013-8000；"
                                               "不要发到 l***@gmail.com，也不要联系 139****4321")


def test_text_with_nothing_to_mask_is_kept_as_typed(client, ask, login):
    emma = login("customer@leon.demo")
    r = client.post("/api/conversations", json={"title": "ＳＯ订单 退款（加急）"}, headers=emma)
    assert r.json()["title"] == "ＳＯ订单 退款（加急）", "width folding applies only to text that had something masked"


def test_a_masked_number_is_not_a_fact_the_answer_must_contain():
    assert key_facts("退款1-3个工作日到账，回访电话 139****4321") == ["1", "3个工作日"]
    assert key_facts("回访电话139****4321到账3天") == ["3天"], "only the masked token goes, not the whole clause"
    assert key_facts("发到l***@gmail.com后3个工作日") == ["3个工作日"]


def test_staff_read_the_masked_comment(client, ask, login):
    emma = login("customer@leon.demo")
    conversation_id, _ = ask(emma, "退款多久到账？")
    message_id = _assistant_message(client, emma, conversation_id)
    client.post(f"/api/messages/{message_id}/feedback", json={"rating": "down", "comment": PII}, headers=emma)
    [row] = client.get("/api/flywheel/feedback", headers=login("admin@leon.demo")).json()
    assert row["comment"] == "打我电话139****4321，身份证3201**********4321，卡号6217***********2345"


def test_masking_turned_off_keeps_the_text(settings, client, ask, login):
    settings.pii_masking = False
    emma = login("customer@leon.demo")
    conversation_id, _ = ask(emma, "退款多久到账？")
    message_id = _assistant_message(client, emma, conversation_id)
    client.post(f"/api/messages/{message_id}/feedback", json={"rating": "down", "comment": PII}, headers=emma)
    with new_session() as db:
        assert db.execute(select(Feedback.comment)).scalar_one() == PII


def test_upgrade_masks_rows_stored_before(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    from app.db import BACKEND_DIR, init_engine
    from tests.conftest import _reset_database
    from tests.test_migrations import _url

    from importlib import import_module

    fields = import_module("migrations.versions.0023_mask_stored_free_text").FIELDS
    for table, columns, _ in fields:  # raw SQL: the names are checked here, not by the ORM
        assert set(columns) <= set(Base.metadata.tables[table].columns.keys()), table
    monkeypatch.delenv("LOOPDESK_PII_MASKING", raising=False)
    url = _url(tmp_path, "masking.db")
    _reset_database(url)
    init_engine(url)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0022")
    with get_engine().begin() as conn:
        conn.exec_driver_sql("INSERT INTO tenants (id, slug, name, industry, auto_refund_limit, ticket_sla_hours, "
                             "return_window_days, quality_window_days, created_at) "
                             "VALUES (1, 't', 'T', 'x', 50, 24, 7, 15, CURRENT_TIMESTAMP)")
        conn.exec_driver_sql("INSERT INTO users (id, tenant_id, email, name, role, password_hash, created_at) "
                             "VALUES (7, 1, 'c@t', 'C', 'customer', 'x', CURRENT_TIMESTAMP)")
        conn.exec_driver_sql("INSERT INTO conversations (id, tenant_id, user_id, title, channel, status, created_at, "
                             f"updated_at) VALUES (3, 1, 7, '找 {PHONE}', 'web', 'bot', CURRENT_TIMESTAMP, "
                             "CURRENT_TIMESTAMP)")
        for message_id, role in ((11, "user"), (12, "agent")):
            conn.exec_driver_sql("INSERT INTO messages (id, tenant_id, conversation_id, role, content, meta, "
                                 f"created_at) VALUES ({message_id}, 1, 3, '{role}', '{PII}', '{{}}', CURRENT_TIMESTAMP)")
        conn.exec_driver_sql("INSERT INTO feedback (tenant_id, message_id, user_id, rating, question, answer, "
                             "correction, reason_category, attribution, comment, created_at) "
                             f"VALUES (1, 12, 7, 'down', '', '', '', '', '', '{PII}', CURRENT_TIMESTAMP)")
        conn.exec_driver_sql("INSERT INTO channel_events (channel, event_id, received_at, status, attempts, payload) "
                             f"VALUES ('feishu', 'e1', CURRENT_TIMESTAMP, 'done', 1, "
                             f"'{{\"tenant\": \"t\", \"message_id\": \"om_1\", \"text\": \"{PII}\"}}')")
        conn.exec_driver_sql("INSERT INTO conversations (id, tenant_id, user_id, title, channel, status, created_at, "
                             "updated_at) VALUES (4, 1, 7, '回电１３９８７６５４３２１', 'web', 'bot', CURRENT_TIMESTAMP, "
                             "CURRENT_TIMESTAMP)")
        conn.exec_driver_sql("INSERT INTO feedback (tenant_id, message_id, user_id, rating, question, answer, "
                             "correction, reason_category, attribution, comment, dedup_key, created_at) "
                             f"VALUES (1, 12, 7, 'correction', '', '', '回访 {PHONE}', '', '', '', 'old', "
                             "CURRENT_TIMESTAMP)")
    command.upgrade(config, "head")
    with get_engine().connect() as conn:
        titles = dict(conn.exec_driver_sql("SELECT id, title FROM conversations").all())
        assert titles == {3: "找 139****4321", 4: "回电139****4321"}, "full-width digits are folded first"
        payload = conn.exec_driver_sql("SELECT payload FROM channel_events").scalar_one()
        assert PHONE not in str(payload) and "om_1" in str(payload), "a settled event keeps what spots a redelivery"
        assert conn.exec_driver_sql("SELECT comment FROM feedback WHERE rating = 'down'").scalar_one().startswith("打我电话139****4321，")
        contents = dict(conn.exec_driver_sql("SELECT role, content FROM messages").all())
        correction, key = conn.exec_driver_sql("SELECT correction, dedup_key FROM feedback "
                                               "WHERE rating = 'correction'").one()
    assert correction == "回访 139****4321"
    assert key == hashlib.sha256(f"12:7:correction:{correction}".encode()).hexdigest(), "a resend is still recognised"
    assert PHONE not in contents["agent"]
    assert contents["user"] == PII, "customer messages are masked when written; the upgrade leaves them alone"
