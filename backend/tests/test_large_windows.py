"""N-133: the dashboard and the exports put every id in the window into one ``IN (...)``, one bound
parameter each. SQLite refuses a statement with more than 32,766 of them (PostgreSQL 65,535), so a
tenant with about 4,700 conversations a day got a 500 from the 7-day dashboard. The window is now a
subquery; these tests fill it past the limit."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select

from app.db import new_session
from app.models import Conversation, Run, Span, Tenant, Ticket, User

PAST_THE_LIMIT = 33_000  # SQLite's SQLITE_MAX_VARIABLE_NUMBER is 32,766


def _fill(handoffs: bool = False) -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=5)
    with new_session() as db:
        tenant_id = db.execute(select(Tenant.id).where(Tenant.slug == "leon")).scalar_one()
        user_id = db.execute(select(User.id).where(User.email == "customer@leon.demo")).scalar_one()
        db.execute(insert(Run), [
            {"id": f"bulk{i:08d}", "tenant_id": tenant_id, "user_id": user_id, "status": "completed",
             "input_text": "退款多久到账", "output_text": "1-3个工作日", "latency_ms": 900, "created_at": now}
            for i in range(PAST_THE_LIMIT)])
        db.execute(insert(Span), [
            {"run_id": "bulk00000000", "tenant_id": tenant_id, "kind": "tool", "name": "get_order"},
            {"run_id": "bulk00000001", "tenant_id": tenant_id, "kind": "guardrail", "name": "knowledge_conflict"}])
        if handoffs:
            conversation_id = db.execute(insert(Conversation).values(
                tenant_id=tenant_id, user_id=user_id).returning(Conversation.id)).scalar_one()
            db.execute(insert(Ticket), [
                {"tenant_id": tenant_id, "customer_id": user_id, "title": "转人工", "kind": "handoff",
                 "status": "closed", "conversation_id": conversation_id, "created_at": now, "closed_at": now}
                for _ in range(PAST_THE_LIMIT)])
        db.commit()


def test_dashboard_counts_a_window_past_the_parameter_limit(client, login):
    _fill(handoffs=True)
    r = client.get("/api/metrics/overview?days=7", headers=login("admin@leon.demo"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runs"] == PAST_THE_LIMIT
    assert body["tools"] == {"get_order": 1}
    assert body["knowledge_conflicts"]["handoffs"] == 1
    assert body["handoff_queue"]["handoffs"] == PAST_THE_LIMIT


def test_exports_past_the_parameter_limit(client, login):
    _fill(handoffs=True)
    admin = login("admin@leon.demo")
    runs = client.get("/api/export/runs.csv?days=7", headers=admin)
    assert runs.status_code == 200, runs.text
    lines = runs.content.decode("utf-8-sig").splitlines()
    assert len(lines) == PAST_THE_LIMIT + 1
    assert lines[1].startswith("bulk00000000,") and lines[1].endswith(",get_order")
    handoffs = client.get("/api/export/handoffs.csv?days=7", headers=admin)
    assert handoffs.status_code == 200, handoffs.text
    assert len(handoffs.content.decode("utf-8-sig").splitlines()) == PAST_THE_LIMIT + 1


def test_the_window_stays_tenant_scoped(client, login):
    _fill()
    body = client.get("/api/metrics/overview", headers=login("admin@iris.demo")).json()
    assert body["runs"] == 0 and body["tools"] == {} and body["knowledge_conflicts"]["handoffs"] == 0


def test_eval_runs_and_old_runs_stay_out_of_the_counts(client, login):
    """The window's filters now live in the subquery: an eval run's or an old run's spans must not count."""
    with new_session() as db:
        tenant_id = db.execute(select(Tenant.id).where(Tenant.slug == "leon")).scalar_one()
        user_id = db.execute(select(User.id).where(User.email == "customer@leon.demo")).scalar_one()
        old = datetime.now(timezone.utc) - timedelta(days=30)
        db.execute(insert(Run), [
            {"id": "evalrun", "tenant_id": tenant_id, "user_id": user_id, "status": "completed", "is_eval": True},
            {"id": "oldrun", "tenant_id": tenant_id, "user_id": user_id, "status": "completed", "created_at": old}])
        db.execute(insert(Span), [{"run_id": r, "tenant_id": tenant_id, "kind": k, "name": n}
                                  for r in ("evalrun", "oldrun")
                                  for k, n in (("tool", "get_order"), ("guardrail", "knowledge_conflict"))])
        db.commit()
    admin = login("admin@leon.demo")
    body = client.get("/api/metrics/overview?days=7", headers=admin).json()
    assert body["runs"] == 0 and body["tools"] == {} and body["knowledge_conflicts"]["handoffs"] == 0
    lines = client.get("/api/export/runs.csv?days=7", headers=admin).content.decode("utf-8-sig").splitlines()
    assert len(lines) == 1
