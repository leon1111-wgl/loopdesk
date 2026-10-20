"""A customer who closes the tab mid-answer must not leave a run stranded."""

import json
import threading
import time
from contextlib import closing

import httpx
import pytest
from sqlalchemy import func, select

from app.agent import answer_cache
from app.api.chat import detached
from app.db import new_session
from app.evals.load_bench import _prepare_database, _start_server
from app.models import Approval, Conversation, Refund, Run, Tenant, User
from app.services import get_services
from tests.conftest import by_type


def test_a_detached_run_finishes_after_the_reader_leaves():
    finished = threading.Event()

    def produce():
        for i in range(5):
            yield {"i": i}
            time.sleep(0.01)
        finished.set()

    stream = detached(produce)
    assert next(stream) == {"i": 0}
    stream.close()  # the client went away
    assert finished.wait(2), "the run kept going without a reader"


def test_closing_the_tab_mid_answer_still_saves_the_answer(tmp_path):
    # A real server: an in-process test client does not deliver a disconnect the way
    # uvicorn does. The mock streams slowly so the client leaves mid-answer.
    url = f"sqlite:///{tmp_path / 'stream.db'}"
    _prepare_database(url)
    proc, base = _start_server(url, workers=1, cache_hours=0, extra_env={"LOOPDESK_MOCK_STREAM_DELAY": "0.02"})
    try:
        def login(email):
            return {"Authorization": "Bearer " + httpx.post(
                f"{base}/api/auth/login", json={"email": email, "password": "loopdesk-demo"}).json()["token"]}

        emma, leon = login("customer@leon.demo"), login("admin@leon.demo")
        conversation_id = httpx.post(f"{base}/api/conversations", json={}, headers=emma).json()["id"]
        run_id = None
        with httpx.Client(timeout=30) as client:
            with client.stream("POST", f"{base}/api/conversations/{conversation_id}/stream",
                               json={"content": "七天无理由退货有什么条件？"}, headers=emma) as response:
                for line in response.iter_lines():
                    if line.startswith("data: ") and json.loads(line[6:])["type"] == "token":
                        run_id = json.loads(line[6:]).get("run_id") or run_id
                        break  # the answer has started streaming; the customer closes the tab
                    if line.startswith("data: ") and json.loads(line[6:])["type"] == "run_started":
                        run_id = json.loads(line[6:])["run_id"]
        deadline, messages = time.time() + 15, []
        while time.time() < deadline:
            messages = httpx.get(f"{base}/api/conversations/{conversation_id}/messages", headers=emma).json()["messages"]
            if len(messages) == 2:
                break
            time.sleep(0.2)
        assert [m["role"] for m in messages] == ["user", "assistant"], "the answer is waiting when the customer returns"
        assert httpx.get(f"{base}/api/runs/{run_id}", headers=leon).json()["status"] == "completed"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _start_run(db, text="七天无理由退货有什么条件？"):
    user = db.execute(select(User).where(User.email == "customer@leon.demo")).scalar_one()
    conversation = Conversation(tenant_id=user.tenant_id, user_id=user.id)
    db.add(conversation)
    db.commit()
    return get_services().runtime(db).start(tenant=db.get(Tenant, user.tenant_id), user=user, text=text,
                                            conversation=conversation)


@pytest.mark.parametrize("stop_at", ["run_started", "token"])
def test_a_reader_that_stops_early_does_not_leave_the_run_running(client, stop_at):
    # The stream endpoint drains every run in its own thread (above), but the runtime is a
    # generator that any channel or script can read; one that stops early must end the run.
    with new_session() as db:
        events = _start_run(db)
        run_id = next(events)["run_id"]
        if stop_at == "token":
            while next(events)["type"] != "token":
                pass
        with pytest.raises(GeneratorExit):  # what close() sends: it must come back out, not be swallowed
            events.throw(GeneratorExit)
    with new_session() as db:
        run = db.get(Run, run_id)
        assert run.status == "failed" and "client disconnected" in run.error and run.finished_at is not None


def test_a_reader_that_stops_after_the_answer_leaves_the_run_completed(client):
    with new_session() as db:
        events = _start_run(db)
        run_id = next(events)["run_id"]
        while next(events)["type"] != "final":
            pass
        events.close()  # "done" is never read, but the run was saved before "final" was sent
    with new_session() as db:
        run = db.get(Run, run_id)
        assert run.status == "completed" and run.error is None


def _read_until_approval_required(events) -> tuple[str, int]:
    run_id = next(events)["run_id"]
    while (event := next(events))["type"] != "approval_required":
        pass
    return run_id, event["approval_id"]


def test_a_reader_that_stops_at_approval_required_leaves_an_approval_that_can_be_approved(client, login):
    # A reader may stop once it knows a person has to decide. The run used to be saved only
    # after this event, so it was marked failed with no pending call to resume: every approve
    # then failed (KeyError 'pending') and put the approval back to pending, for good.
    with new_session() as db:
        events = _start_run(db, "SO20261008002 我要退款")
        run_id, approval_id = _read_until_approval_required(events)
        events.close()
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"},
                    headers=login("admin@leon.demo"))
    assert r.status_code == 200, r.text
    with new_session() as db:
        run = db.get(Run, run_id)
        assert run.status == "completed" and run.error is None
        assert db.execute(select(func.count(Refund.id)).where(Refund.order_id == "SO20261008002")).scalar_one() == 1
        conversation_id = run.conversation_id
    messages = client.get(f"/api/conversations/{conversation_id}/messages",
                          headers=login("customer@leon.demo")).json()["messages"]
    answers = [m for m in messages if m["role"] == "assistant"]
    assert [m["meta"]["status"] for m in answers] == ["waiting_approval", "completed"]
    assert "已受理" in answers[-1]["content"], "the customer gets the resumed answer"


def test_a_reader_that_stops_while_a_reviewer_resumes_leaves_the_run_to_the_reviewer(client, monkeypatch):
    # Once paused, the run is the reviewer's. A reader that stops while a reviewer resumes it
    # (the row says "running" again) must not mark it failed, or the completed run keeps the
    # error "client disconnected". This holds because no transaction is open while the pause's
    # events are read, so _mark_failed's rollback reloads nothing: it sees the run as paused.
    services = get_services()
    refund = services.registry.get("request_refund")
    pay = refund.handler
    with new_session() as db, closing(_start_run(db, "SO20261008002 我要退款")) as events:
        run_id, approval_id = _read_until_approval_required(events)

        def reader_stops_mid_resume(ctx, args):
            events.close()
            return pay(ctx, args)

        monkeypatch.setattr(refund, "handler", reader_stops_mid_resume)
        with new_session() as reviewer_db:
            admin = reviewer_db.execute(select(User).where(User.email == "admin@leon.demo")).scalar_one()
            resumed = services.runtime(reviewer_db).resume(reviewer_db.get(Approval, approval_id), "approve", admin)
    assert "已受理" in by_type(resumed, "final")[0]["text"]
    with new_session() as db:
        run = db.get(Run, run_id)
        assert run.status == "completed" and run.error is None, (run.status, run.error)


def test_a_crash_before_the_agent_loop_still_ends_the_run(ask, login, monkeypatch):
    def locked(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(answer_cache, "lookup", locked)  # checked before the loop starts
    _, events = ask(login("customer@leon.demo"), "七天无理由退货有什么条件？")
    assert by_type(events, "error")
    with new_session() as db:
        run = db.get(Run, events[0]["run_id"])
        assert run.status == "failed" and "database is locked" in run.error
