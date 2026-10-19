from datetime import datetime, timedelta, timezone

from app.db import new_session
from app.models import Approval, Message, Run, Span
from app.ops.retention import REDACTED, apply_retention
from tests.conftest import by_type


def _age(db, run_id: str, days: int) -> None:
    old = datetime.now(timezone.utc) - timedelta(days=days)
    run = db.get(Run, run_id)
    run.created_at = old
    for m in db.query(Message).filter(Message.conversation_id == run.conversation_id):
        m.created_at = old
    db.commit()


def test_old_conversations_are_redacted_and_recent_ones_kept(ask, login):
    customer = login("customer@leon.demo")
    _, old_events = ask(customer, "积分可以怎么抵扣？")
    _, new_events = ask(customer, "付款以后多久发货？")
    old_run, new_run = by_type(old_events, "done")[0]["run_id"], by_type(new_events, "done")[0]["run_id"]
    with new_session() as db:
        _age(db, old_run, 120)
        assert apply_retention(db, 90, dry_run=True)["runs"] == 1
        counts = apply_retention(db, 90)
        assert counts["runs"] == 1 and counts["spans"] > 0 and counts["messages"] == 2
        run = db.get(Run, old_run)
        assert run.input_text == REDACTED and run.prompt_tokens > 0, "metrics survive, text does not"
        assert db.query(Span).filter(Span.run_id == old_run).count() == 0
        assert db.get(Run, new_run).input_text != REDACTED
        assert apply_retention(db, 90)["runs"] == 0, "idempotent"


def test_runs_waiting_for_approval_are_never_redacted(ask, login):
    _, events = ask(login("customer@leon.demo"), "SO20261008002 我要退款")
    run_id = by_type(events, "done")[0]["run_id"]
    with new_session() as db:
        _age(db, run_id, 120)
        apply_retention(db, 90)
        assert db.get(Run, run_id).input_text != REDACTED
        assert db.query(Approval).filter(Approval.run_id == run_id).count() == 1, "audit records stay"


def test_erase_user_personal_data(client, ask, login):
    from app.models import Order, User

    customer = login("customer@leon.demo")
    _, events = ask(customer, "SO20261008002 我要退款")
    run_id = by_type(events, "done")[0]["run_id"]
    other_run = by_type(ask(login("customer2@leon.demo"), "积分可以怎么抵扣？")[1], "done")[0]["run_id"]
    with new_session() as db:
        emma = db.query(User).filter(User.email == "customer@leon.demo").one().id
    admin = login("admin@leon.demo")
    assert client.delete(f"/api/admin/users/{emma}/personal-data", headers=login("admin@iris.demo")).status_code == 404
    assert client.delete(f"/api/admin/users/{emma}/personal-data", headers=login("agent@leon.demo")).status_code == 403
    erased = client.delete(f"/api/admin/users/{emma}/personal-data", headers=admin).json()["erased"]
    assert erased["messages"] == 2 and erased["runs"] == 1 and erased["approvals"] == 1
    assert client.post("/api/auth/login", json={"email": "customer@leon.demo", "password": "loopdesk-demo"}).status_code == 401
    with new_session() as db:
        assert db.get(Run, run_id).input_text == REDACTED
        approval = db.query(Approval).filter(Approval.run_id == run_id).one()
        assert approval.evidence == [] and approval.arguments["amount"] == 1299.0, "audit record kept, quotes removed"
        assert db.get(Order, "SO20261008002") is not None, "orders are accounting records"
        assert db.get(Run, other_run).input_text != REDACTED, "other customers untouched"
