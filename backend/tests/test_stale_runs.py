"""A process killed mid-run leaves its run "running"; app/ops/stale_runs.py ends it."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select, update

from app.config import Settings
from app.db import new_session
from app.evals.load_bench import _start_server
from app.main import create_app
from app.models import Approval, Conversation, Message, Refund, Run, User
from app.ops import stale_runs
from app.ops.stale_runs import INTERRUPTED_ERROR, INTERRUPTED_TEXT, fail_stale_runs, stale_after
from app.seed import DEMO_PASSWORD
from app.services import get_services
from tests.conftest import by_type


class Killed(BaseException):
    """kill -9, in-process: nothing in the app catches a BaseException, so the rows stay as the
    process last committed them. Unlike a real kill, finally blocks still run; none ends a run."""


@pytest.fixture()
def exported(settings, client):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app import telemetry

    exporter = InMemorySpanExporter()
    telemetry.use_processor(settings, SimpleSpanProcessor(exporter))
    yield exporter
    telemetry.use_processor(settings, None)


def _left_running(db, *, age: timedelta, status: str = "running", channel: str = "web",
                  conversation_id: int | None = None) -> tuple[str, int]:
    """What AgentRuntime.start() has committed when its process dies: the customer's message
    and the run, ``age`` ago."""
    user = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
    if conversation_id is None:
        conversation = Conversation(tenant_id=user.tenant_id, user_id=user.id, channel=channel)
        db.add(conversation)
        db.flush()
        conversation_id = conversation.id
    started = datetime.now(timezone.utc) - age
    db.add(Message(tenant_id=user.tenant_id, conversation_id=conversation_id, role="user",
                   content="积分可以怎么抵扣？", created_at=started))
    run = Run(tenant_id=user.tenant_id, conversation_id=conversation_id, user_id=user.id,
              input_text="积分可以怎么抵扣？", status=status, created_at=started)
    db.add(run)
    db.commit()
    return run.id, conversation_id


def _notices(db, conversation_id: int) -> list[str]:
    return db.execute(select(Message.content).where(Message.conversation_id == conversation_id,
                                                    Message.role == "system")).scalars().all()


def test_a_run_killed_with_its_process_is_ended_and_the_customer_told(settings, client, login):
    # A real server, killed (SIGKILL) while it streams an answer: no except or finally in it runs.
    proc, base = _start_server(settings.database_url, workers=1, cache_hours=0,
                               extra_env={"LOOPDESK_MOCK_STREAM_DELAY": "0.05"})
    try:
        token = httpx.post(f"{base}/api/auth/login",
                           json={"email": "customer@leon.demo", "password": DEMO_PASSWORD}).json()["token"]
        emma = {"Authorization": f"Bearer {token}"}
        conversation_id = httpx.post(f"{base}/api/conversations", json={}, headers=emma).json()["id"]
        run_id = None
        with httpx.stream("POST", f"{base}/api/conversations/{conversation_id}/stream", headers=emma,
                          json={"content": "七天无理由退货有什么条件？"}, timeout=30) as response:
            for line in response.iter_lines():
                event = json.loads(line[6:]) if line.startswith("data: ") else {}
                run_id = event.get("run_id", run_id)
                if event.get("type") == "token":  # mid-answer
                    proc.kill()
                    proc.wait(timeout=10)
                    break
    finally:
        proc.kill()
        proc.wait(timeout=10)

    with new_session() as db:
        assert db.get(Run, run_id).status == "running", "nothing in the killed process ended it"
    assert fail_stale_runs(settings) == {"failed": 0, "notified": 0}, "too recent: another instance may be running it"
    later = datetime.now(timezone.utc) + stale_after(settings) + timedelta(seconds=1)
    assert fail_stale_runs(settings, now=later) == {"failed": 1, "notified": 1}
    with new_session() as db:
        run = db.get(Run, run_id)
        assert run.status == "failed" and run.error == INTERRUPTED_ERROR and run.finished_at is not None
    messages = client.get(f"/api/conversations/{conversation_id}/messages",
                          headers=login("customer@leon.demo")).json()["messages"]
    assert [(m["role"], m["content"]) for m in messages][1:] == [("system", INTERRUPTED_TEXT)]
    assert fail_stale_runs(settings, now=later) == {"failed": 0, "notified": 0}


def test_only_runs_running_past_the_cutoff_are_ended(client, settings):
    limit = stale_after(settings)
    with new_session() as db:
        abandoned, _ = _left_running(db, age=limit + timedelta(minutes=1))
        slow, _ = _left_running(db, age=limit - timedelta(minutes=1))  # e.g. a live instance's slow run
        paused, _ = _left_running(db, age=timedelta(days=3), status="waiting_approval")
        done, _ = _left_running(db, age=timedelta(days=3), status="completed")
    assert fail_stale_runs(settings) == {"failed": 1, "notified": 1}
    with new_session() as db:
        statuses = {run_id: db.get(Run, run_id).status for run_id in (abandoned, slow, paused, done)}
        assert statuses == {abandoned: "failed", slow: "running", paused: "waiting_approval", done: "completed"}
        assert db.get(Run, abandoned).error == INTERRUPTED_ERROR


def test_a_resumed_run_counts_from_the_decision_not_from_the_question(client, settings, ask, login, monkeypatch):
    # resume() sets a run paused for approval back to "running" while it carries out the
    # decision; the run itself dates from the question, here two days earlier.
    customer = login("customer@leon.demo")
    conversation_id, events = ask(customer, "SO20261008002 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
    with new_session() as db:
        run_id = db.get(Approval, approval_id).run_id
        db.execute(update(Run).where(Run.id == run_id).values(created_at=two_days_ago))
        db.commit()

    def killed(*args, **kwargs):  # the refund has been paid; the process dies while the answer is written
        raise Killed

    monkeypatch.setattr(get_services().provider, "stream", killed)
    with new_session() as db, pytest.raises(Killed):
        admin = db.execute(select(User).where(User.email == "admin@leon.demo")).scalar_one()
        get_services().runtime(db).resume(db.get(Approval, approval_id), "approve", admin)
    monkeypatch.undo()
    with new_session() as db:
        assert db.get(Run, run_id).status == "running"
        assert db.execute(select(func.count(Refund.id)).where(Refund.approval_id == approval_id)).scalar_one() == 1

    assert fail_stale_runs(settings) == {"failed": 0, "notified": 0}, "decided just now: the answer may be on its way"
    later = datetime.now(timezone.utc) + stale_after(settings) + timedelta(seconds=1)
    assert fail_stale_runs(settings, now=later) == {"failed": 1, "notified": 1}
    messages = client.get(f"/api/conversations/{conversation_id}/messages", headers=customer).json()["messages"]
    assert messages[-2]["meta"]["status"] == "waiting_approval", "what the customer's page was polling on"
    assert (messages[-1]["role"], messages[-1]["content"]) == ("system", INTERRUPTED_TEXT)


def test_overlapping_sweeps_end_each_run_once(client, settings, monkeypatch, exported):
    with new_session() as db:
        runs = [_left_running(db, age=stale_after(settings) + timedelta(minutes=1)) for _ in range(2)]
    claim, second = stale_runs._claim, {}

    def claim_after_another_sweep(db, run_id, cutoff):
        if not second:  # the next cron run starts after this one looked the runs up, before it ends them
            second["started"] = True
            second["counts"] = fail_stale_runs(settings)
        return claim(db, run_id, cutoff)

    monkeypatch.setattr(stale_runs, "_claim", claim_after_another_sweep)
    assert fail_stale_runs(settings) == {"failed": 0, "notified": 0}
    assert second["counts"] == {"failed": 2, "notified": 2}
    with new_session() as db:
        assert [_notices(db, conversation_id) for _, conversation_id in runs] == [[INTERRUPTED_TEXT]] * 2
    roots = [s for s in exported.get_finished_spans() if s.parent is None]
    assert sorted(s.attributes["loopdesk.run.id"] for s in roots) == sorted(run_id for run_id, _ in runs)
    assert {(s.attributes["loopdesk.run.status"], s.status.description) for s in roots} == {("failed", "interrupted")}


@pytest.mark.parametrize("situation", ["feishu", "handoff", "asked_again"])
def test_no_notice_when_the_customer_is_not_waiting_on_that_run(client, settings, situation):
    with new_session() as db:
        run_id, conversation_id = _left_running(db, age=stale_after(settings) + timedelta(minutes=1),
                                                channel="feishu" if situation == "feishu" else "web")
        if situation == "handoff":  # a person has the conversation now
            db.get(Conversation, conversation_id).status = "handoff"
            db.commit()
        if situation == "asked_again":  # the customer sent it again; that run is young, maybe still going
            _left_running(db, age=timedelta(seconds=5), conversation_id=conversation_id)
    assert fail_stale_runs(settings) == {"failed": 1, "notified": 0}
    with new_session() as db:
        assert db.get(Run, run_id).status == "failed" and _notices(db, conversation_id) == []


def test_the_dashboard_shows_an_ended_run_as_interrupted_not_running(client, settings, login):
    with new_session() as db:
        _left_running(db, age=stale_after(settings) + timedelta(minutes=1))
    admin = login("admin@leon.demo")
    assert client.get("/api/metrics/overview", headers=admin).json()["status"].get("running") == 1
    fail_stale_runs(settings)
    overview = client.get("/api/metrics/overview", headers=admin).json()
    assert "running" not in overview["status"]
    assert overview["failures"]["kinds"] == {"interrupted": 1} and overview["failures"]["last_15m"] == 1


def test_an_instance_starting_ends_only_runs_abandoned_long_ago(client, settings):
    with new_session() as db:
        old, _ = _left_running(db, age=stale_after(settings) + timedelta(minutes=1))
        recent, _ = _left_running(db, age=timedelta(seconds=5))  # e.g. from the crash that caused this restart
    create_app(settings)
    with new_session() as db:
        assert db.get(Run, old).status == "failed"
        assert db.get(Run, recent).status == "running", "maybe another instance's live run"


def test_the_cutoff_covers_what_the_run_deadline_does_not():
    defaults = Settings()
    assert stale_after(defaults) == timedelta(seconds=290)  # 45 s + 5 s rewrite + 3 x 60 s judge + 60 s
    defaults.run_deadline_seconds = 0  # disabled: as long as six steps on both model tiers can take
    assert stale_after(defaults) > timedelta(minutes=30)
