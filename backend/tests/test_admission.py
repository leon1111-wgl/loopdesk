"""A full process turns chat requests away at once instead of queueing them into a 500 (N-158).

The burst numbers (a 6 s fake model, 60–200 requests at once) are in docs/PERFORMANCE.md;
``python -m app.evals.load_bench --model-latency 6`` reproduces them."""

import threading

import pytest
from anyio import to_thread
from fastapi import HTTPException
from sqlalchemy import func, select

from app.admission import OVERLOADED, Admission, get_admission
from app.api.chat import detached
from app.db import new_session
from app.models import Conversation


def test_a_full_admission_turns_away_with_retry_after_and_counts_it():
    admission = Admission(limit=2, retry_after=5)
    admission.enter()
    admission.enter()
    with pytest.raises(HTTPException) as caught:
        admission.enter()
    assert caught.value.status_code == 503
    assert caught.value.headers == {"Retry-After": "5"}
    assert (admission.inflight, admission.rejected) == (2, 1)
    admission.leave()
    admission.enter()  # a finished run frees its slot
    assert admission.inflight == 2


@pytest.fixture()
def one_slot(settings):
    settings.max_inflight_runs = 1
    return settings


def _conversations() -> int:
    with new_session() as db:
        return db.execute(select(func.count(Conversation.id))).scalar_one()


def test_chat_and_stream_get_503_while_the_process_is_full(one_slot, client, login):
    emma = login("customer@leon.demo")
    conversation_id = client.post("/api/conversations", json={}, headers=emma).json()["id"]
    admission = get_admission()
    admission.enter()  # another customer's answer is still waiting on the model
    try:
        before = _conversations()
        r = client.post("/api/chat", json={"content": "退款多久能到账？"}, headers=emma)
        assert r.status_code == 503 and r.headers["retry-after"] == "5"
        assert r.json()["detail"] == OVERLOADED
        assert _conversations() == before  # a turned-away request leaves no empty conversation
        r = client.post(f"/api/conversations/{conversation_id}/stream", json={"content": "退款多久能到账？"},
                        headers=emma)
        assert r.status_code == 503 and r.headers["retry-after"] == "5"
        metrics = client.get("/metrics").text
        assert "loopdesk_inflight_runs 1\n" in metrics
        assert "loopdesk_inflight_runs_limit 1\n" in metrics
        assert "loopdesk_runs_rejected_total 2\n" in metrics
    finally:
        admission.leave()

    # Once the other answer is done, both paths answer again and give their slot back.
    r = client.post("/api/chat", json={"content": "退款多久能到账？"}, headers=emma)
    assert r.status_code == 200 and r.json()["answer"]
    r = client.post(f"/api/conversations/{conversation_id}/stream", json={"content": "积分可以怎么抵扣？"},
                    headers=emma)
    assert r.status_code == 200 and '"type": "final"' in r.text
    assert admission.inflight == 0


def test_a_run_that_fails_still_gives_its_slot_back(one_slot, client, login, monkeypatch):
    from app.agent import runtime

    def broken(*args, **kwargs):
        raise RuntimeError("model client crashed")

    monkeypatch.setattr(runtime.AgentRuntime, "start", broken)
    emma = login("customer@leon.demo")
    conversation_id = client.post("/api/conversations", json={}, headers=emma).json()["id"]
    assert client.post("/api/chat", json={"content": "退款多久能到账？"}, headers=emma).status_code == 500
    r = client.post(f"/api/conversations/{conversation_id}/stream", json={"content": "退款多久能到账？"},
                    headers=emma)
    assert '"type": "error"' in r.text
    assert get_admission().inflight == 0


def test_a_stream_nobody_reads_still_runs_and_frees_its_slot():
    # The slot is taken in the request; if the response never starts, only the run can give it back.
    finished = threading.Event()

    def produce():
        yield {"i": 0}
        finished.set()

    detached(produce)  # never read
    assert finished.wait(2)


def test_health_and_worker_threads_do_not_depend_on_the_chat_load(settings, client):
    # Liveness runs on the event loop: with every worker thread waiting on the model it still answers.
    route = next(r for r in client.app.routes if getattr(r, "path", "") == "/api/health")
    assert route.dependant.call.__code__.co_flags & 0x80  # CO_COROUTINE
    limit = client.portal.call(lambda: to_thread.current_default_thread_limiter().total_tokens)
    assert limit == settings.max_inflight_runs + 40


@pytest.fixture()
def slow_stream(settings):
    settings.mock_stream_delay = 0.05  # read when the app is built, so before `client`
    return settings


def test_a_stream_does_not_hold_a_database_connection_while_it_runs(slow_stream, client, login):
    # The request session's dependencies (user lookup, conversation check) open a transaction; FastAPI
    # closes that session only after the response, so without closing it first every stream kept a
    # connection for its whole run and 16 streams at once emptied the default pool (5 + 10).
    from app.db import get_engine

    emma = login("customer@leon.demo")
    conversation_id = client.post("/api/conversations", json={}, headers=emma).json()["id"]
    checked_out = []
    with client.stream("POST", f"/api/conversations/{conversation_id}/stream",
                       json={"content": "七天无理由退货有什么条件？"}, headers=emma) as response:
        for line in response.iter_lines():
            if line.startswith("data:") and '"type": "token"' in line:
                checked_out.append(get_engine().pool.checkedout())
    assert checked_out and max(checked_out) <= 1  # the run's own session, between its commits
