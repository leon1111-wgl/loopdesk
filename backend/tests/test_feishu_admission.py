"""Feishu events take admission slots like chat messages do, and wait instead of being refused
(N-189). Before, a flood of 60 events ran 48 answers at once on an instance admitting 8, and
``loopdesk_inflight_runs`` said 0."""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import admission as admission_module
from app.channels import feishu
from app.channels.feishu import FeishuClient, process_event, retry_stuck
from app.services import get_services
from tests.test_providers_and_channels import _accept_without_answering, _feishu_event, _stored_events


@pytest.fixture(autouse=True)
def _own_admission(monkeypatch):
    monkeypatch.setattr(admission_module, "_admission", admission_module.get_admission())  # restored after


@pytest.fixture()
def gate(client, monkeypatch):
    """Answers that block until released, counting how many run at once."""
    monkeypatch.setattr(feishu, "waiting", feishu._Waiting())
    monkeypatch.setattr(feishu, "QUEUE_POLL", 0.02)
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: {"sent": False})
    state = {"now": 0, "peak": 0, "seen": [], "release": threading.Event(), "lock": threading.Lock()}
    answer = feishu._answer

    def slow(services, tenant_slug, parsed):
        with state["lock"]:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            state["seen"].append(admission_module.get_admission().inflight)
        try:
            state["release"].wait(15)
            return answer(services, tenant_slug, parsed)
        finally:
            with state["lock"]:
                state["now"] -= 1

    monkeypatch.setattr(feishu, "_answer", slow)
    return state


def _event(event_id: str, open_id: str) -> None:
    from app.api.channels import _accept
    from app.channels.feishu import parse_text_event

    body = _feishu_event(event_id)
    body["event"]["sender"]["sender_id"]["open_id"] = open_id
    assert _accept(event_id, {**parse_text_event(body), "tenant": "leon"})


def _wait_for(check, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_a_flood_of_feishu_events_runs_no_more_than_the_admission_limit(gate):
    admission = admission_module.configure(2, tenant_share=1.0, per_user=4)
    for i in range(10):
        _event(f"evt-flood-{i}", f"ou_flood{i % 3}")
    # What the background tasks do: each tries once, as Feishu's deliveries arrive together.
    tasks = [threading.Thread(target=process_event, args=(get_services(), f"evt-flood-{i}")) for i in range(10)]
    for t in tasks:
        t.start()
    _wait_for(lambda: gate["now"] == 2 and feishu.waiting.pending == 8)
    assert admission.inflight == 2, "/metrics sees the Feishu runs"
    assert sum(status == "queued" for status, _ in _stored_events().values()) == 8
    assert admission.rejections == {("process", "1"): 8}, "each queued event counted once, not every poll"
    gate["release"].set()
    _wait_for(lambda: all(status == "done" for status, _ in _stored_events().values()))
    for t in tasks:
        t.join()
    assert gate["peak"] == 2 and max(gate["seen"]) == 2
    assert all(attempts == 1 for _, attempts in _stored_events().values())
    _wait_for(lambda: admission.inflight == 0)
    assert admission.rejections == {("process", "1"): 8}, "admitting the queue refused nothing new"


def test_one_feishu_sender_waits_for_their_own_runs_while_others_go_ahead(gate):
    admission = admission_module.configure(10, tenant_share=1.0, per_user=1)
    for event_id, open_id in (("evt-u1", "ou_same"), ("evt-u2", "ou_same"), ("evt-u3", "ou_other")):
        _event(event_id, open_id)
    tasks = [threading.Thread(target=process_event, args=(get_services(), e)) for e in ("evt-u1", "evt-u2")]
    for t in tasks:
        t.start()
        _wait_for(lambda: gate["now"] == 1 and (t is tasks[0] or feishu.waiting.pending == 1))
    other = threading.Thread(target=process_event, args=(get_services(), "evt-u3"))
    other.start()
    _wait_for(lambda: gate["now"] == 2)  # another customer is not held up by ou_same's backlog
    assert _stored_events()["evt-u2"][0] == "queued"
    assert admission.rejections == {("user", "1"): 1}
    gate["release"].set()
    _wait_for(lambda: all(status == "done" for status, _ in _stored_events().values()))
    _wait_for(lambda: admission.inflight == 0)


def test_events_a_dead_process_left_queued_are_answered_by_the_sweeper(client, monkeypatch):
    replies = []
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: replies.append(mid) or {"sent": False})
    monkeypatch.setattr(feishu, "waiting", feishu._Waiting())
    admission = admission_module.configure(1, tenant_share=1.0)
    held = admission.enter()
    _accept_without_answering("evt-orphan")
    assert process_event(get_services(), "evt-orphan") == {"queued": True}
    feishu.waiting._events.clear()  # the process holding the queue died
    admission.leave(held)
    now = datetime.now(timezone.utc)
    assert retry_stuck(get_services(), now=now) == {"answered": 0, "gave_up": 0}, "a fresh queued row has an owner"
    assert retry_stuck(get_services(), now=now + timedelta(seconds=31)) == {"answered": 0, "gave_up": 0}, \
        "one slow heartbeat (a 30 s pool wait) does not hand a live queue over"
    assert retry_stuck(get_services(), now=now + timedelta(seconds=91)) == {"answered": 1, "gave_up": 0}
    assert replies == ["om_evt-orphan"] and _stored_events()["evt-orphan"] == ("done", 1)


def test_the_queue_keeps_its_rows_fresh(gate):
    admission_module.configure(1, tenant_share=1.0)
    held = admission_module.get_admission().enter()
    _event("evt-beat", "ou_beat")
    assert process_event(get_services(), "evt-beat") == {"queued": True}
    from sqlalchemy import update

    from app.db import new_session
    from app.models import ChannelEvent
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    with new_session() as db:
        db.execute(update(ChannelEvent).values(claimed_at=old))
        db.commit()
    feishu.waiting._heartbeat()
    assert retry_stuck(get_services()) == {"answered": 0, "gave_up": 0}, "still waiting in a live process"
    admission_module.get_admission().leave(held)
    gate["release"].set()
    _wait_for(lambda: _stored_events()["evt-beat"][0] == "done")
    _wait_for(lambda: admission_module.get_admission().inflight == 0)


def test_metrics_show_the_queued_feishu_events(client, gate):
    admission_module.configure(1, tenant_share=1.0)
    held = admission_module.get_admission().enter()
    _event("evt-m", "ou_m")
    assert process_event(get_services(), "evt-m") == {"queued": True}
    body = client.get("/metrics").text
    assert 'loopdesk_channel_events_queued{channel="feishu"} 1' in body
    assert "loopdesk_inflight_runs 1" in body
    admission_module.get_admission().leave(held)
    gate["release"].set()
    _wait_for(lambda: feishu.waiting.pending == 0 and _stored_events()["evt-m"][0] == "done")


def test_a_decision_that_resumes_a_run_takes_a_slot_too(client, ask, login):
    from app.db import new_session
    from app.models import Approval
    from tests.conftest import by_type

    customer = login("customer@leon.demo")
    _, events = ask(customer, "SO20261008002 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    admin = login("admin@leon.demo")
    admission = admission_module.configure(1, tenant_share=1.0)
    held = admission.enter()
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=admin)
    assert r.status_code == 503 and r.headers["Retry-After"] == "5"
    with new_session() as db:
        assert db.get(Approval, approval_id).status == "pending", "nothing decided: it can be sent again"
    admission.leave(held)
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=admin)
    assert r.status_code == 200 and admission.inflight == 0


def test_an_event_that_never_gets_a_slot_is_told_to_try_again(client, monkeypatch):
    # Review: web chat takes a freed slot as fast as the queue does, so under steady overload a
    # queued event could wait forever while its heartbeat kept the sweeper away.
    replies = []
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: replies.append((mid, text)) or {"sent": False})
    monkeypatch.setattr(feishu, "waiting", feishu._Waiting())
    monkeypatch.setattr(feishu, "QUEUE_POLL", 0.02)
    monkeypatch.setattr(feishu, "max_queue_wait", lambda settings: 0.1)
    admission = admission_module.configure(1, tenant_share=1.0)
    held = admission.enter()
    _event("evt-starved", "ou_starved")
    assert process_event(get_services(), "evt-starved") == {"queued": True}
    _wait_for(lambda: _stored_events()["evt-starved"][0] == "failed")
    assert replies == [("om_evt-starved", admission_module.OVERLOADED)]
    assert feishu.waiting.pending == 0 and admission.inflight == 1, "only the slot held by the test"
    admission.leave(held)


def test_a_slot_is_given_back_when_the_run_cannot_be_started(client, monkeypatch):
    monkeypatch.setattr(feishu, "waiting", feishu._Waiting())
    monkeypatch.setattr(feishu, "QUEUE_POLL", 30)  # drained by hand below
    admission = admission_module.configure(1, tenant_share=1.0)
    held = admission.enter()
    _event("evt-nothread", "ou_nothread")
    assert process_event(get_services(), "evt-nothread") == {"queued": True}

    def no_thread(*args, **kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(feishu.waiting._pool, "submit", no_thread)  # before the slot frees and wakes the pump
    admission.leave(held)
    with pytest.raises(RuntimeError):
        feishu.waiting.drain()
    assert admission.inflight == 0 and feishu.waiting.pending == 1, "the slot is back and the event keeps its place"
    feishu.waiting._events.clear()


def test_senders_without_an_open_id_do_not_share_one_user_cap(client):
    from app.db import new_session

    with new_session() as db:
        assert feishu._admission_key(db, {"tenant": "leon", "open_id": ""}) == (1, None)
        assert feishu._admission_key(db, {"tenant": "leon", "open_id": "ou_a"}) == (1, "feishu:leon:ou_a")
        assert feishu._admission_key(db, {"tenant": "nobody", "open_id": "ou_a"})[0] is None
