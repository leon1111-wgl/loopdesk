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
