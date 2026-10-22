"""N-141: a reranker that drips its answer. httpx's timeout is per read, so a service sending one
byte every two seconds never tripped it: one "退款多久到账" took 160 s (one rerank in the search,
another in the policy quote after the run deadline), and the breaker recorded both as successes.
The reranker's timeout is now a total budget, a search gives it at most what is left of the run,
and a timeout counts against the breaker only when the service had its whole budget."""

from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import pytest

from app.config import Settings
from app.db import new_session
from app.llm.base import LLMError
from app.llm.breaker import CircuitBreaker
from app.llm.errors import error_kind
from app.models import Run
from app.rag.rerank import GuardedReranker, HTTPReranker, RerankTimeout
from app.services import get_services
from tests.conftest import by_type
from tests.test_rerank import TableReranker, _search, tenant  # noqa: F401  (fixture)


class Drip(httpx.SyncByteStream):
    """The whole answer at once, then a space every ``interval`` seconds: valid JSON, never done."""

    def __init__(self, body: bytes, interval: float, drops: int):
        self.body, self.interval, self.drops = body, interval, drops

    def __iter__(self):
        yield self.body
        for _ in range(self.drops):
            time.sleep(self.interval)
            yield b" "


def _dripping(timeout: float, interval: float = 0.2, drops: int = 25, calls: list | None = None) -> HTTPReranker:
    def handler(request):
        if calls is not None:
            calls.append(time.monotonic())
        n = len(json.loads(request.content)["documents"])
        body = json.dumps({"results": [{"index": i, "relevance_score": 0.5} for i in range(n)]}).encode()
        return httpx.Response(200, stream=Drip(body, interval, drops))

    return HTTPReranker("http://rerank.test/v1", "", "bge-reranker-v2-m3", timeout=timeout,
                        transport=httpx.MockTransport(handler))


def test_a_dripping_reranker_is_cut_off_at_its_total_timeout():
    reranker = _dripping(timeout=0.4)  # the service would take 5 s
    started = time.monotonic()
    with pytest.raises(RerankTimeout) as caught:
        reranker.score("退款多久到账", ["退款到账", "运费"])
    assert time.monotonic() - started < 1.0, "waited for the drip instead of the 0.4 s budget"
    assert caught.value.full
    # the request thread gives up at the next byte, not after the remaining 5 s
    deadline = time.monotonic() + 1.0
    while any(t.name == "rerank" for t in threading.enumerate()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(t.name == "rerank" for t in threading.enumerate())


def test_a_budget_shorter_than_the_timeout_wins_and_a_fast_answer_still_arrives():
    reranker = _dripping(timeout=3.0)
    started = time.monotonic()
    with pytest.raises(RerankTimeout) as caught:
        reranker.score_within("q", ["a"], 0.3)
    assert time.monotonic() - started < 0.9 and not caught.value.full
    quick = _dripping(timeout=3.0, drops=0)
    assert quick.score_within("q", ["a", "b"], 0.5) == [0.5, 0.5]


def test_only_a_slow_service_counts_against_the_breaker():
    settings = Settings()
    settings.llm_breaker_failures = 2
    breaker = CircuitBreaker(settings)
    calls: list[float] = []
    guarded = GuardedReranker(_dripping(timeout=0.2, calls=calls), breaker, "rerank:x")
    for _ in range(3):  # the run's deadline cut these short: the service is not to blame
        with pytest.raises(RerankTimeout):
            guarded.score_within("q", ["a"], 0.05)
    assert breaker.state("rerank:x") == "closed"
    for _ in range(2):  # the whole 0.2 s and still no answer: slow
        with pytest.raises(RerankTimeout):
            guarded.score("q", ["a"])
    assert breaker.state("rerank:x") == "open"
    before = len(calls)
    with pytest.raises(LLMError, match="circuit open"):
        guarded.score("q", ["a"])
    assert len(calls) == before, "an open breaker does not call the service"


def test_a_short_timeout_hands_a_half_open_trial_back():
    settings = Settings()
    settings.llm_breaker_failures = 1
    settings.llm_breaker_cooldown_seconds = 0
    breaker = CircuitBreaker(settings)
    breaker.record("rerank:x", False)
    assert breaker.allow("rerank:x") and breaker.state("rerank:x") == "half_open"
    breaker.release("rerank:x")
    assert breaker.allow("rerank:x"), "the trial was handed back, the next caller makes it"


def test_a_search_skips_the_reranker_when_the_run_is_out_of_time(tenant):  # noqa: F811
    reranker = TableReranker({("发票抬头", "发票抬头"): 0.9})
    hits, debug = _search(tenant, "发票抬头", reranker=reranker, deadline=time.monotonic() - 1)
    assert reranker.calls == [] and "deadline" in debug.rerank_skipped and debug.rerank_error is None
    assert [h.heading for h in hits] == ["发票抬头"], "the coverage gate still answers"
    hits, debug = _search(tenant, "发票抬头", reranker=reranker, deadline=time.monotonic() + 30)
    assert len(reranker.calls) == 1 and hits[0].rerank == pytest.approx(0.9)
    assert debug.rerank_ms is not None and debug.rerank_error is None


def test_a_search_gives_the_reranker_only_what_is_left_of_the_run(tenant):  # noqa: F811
    started = time.monotonic()
    hits, debug = _search(tenant, "发票抬头", reranker=_dripping(timeout=3.0), deadline=started + 0.4)
    assert time.monotonic() - started < 1.0
    assert [h.heading for h in hits] == ["发票抬头"]
    assert debug.rerank_skipped and debug.rerank_error is None, "cut short by the run: not a fault"


def test_a_server_that_sends_headers_and_then_nothing_is_cut_off_too():
    # MockTransport ignores read timeouts; this is a real socket (review of N-141)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    held: list[socket.socket] = []

    def serve():
        conn, _ = server.accept()
        held.append(conn)
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")

    threading.Thread(target=serve, daemon=True).start()
    reranker = HTTPReranker(f"http://127.0.0.1:{server.getsockname()[1]}", "", "m", timeout=0.3)
    started = time.monotonic()
    try:
        with pytest.raises(RerankTimeout) as caught:
            reranker.score("q", ["a"])
        assert time.monotonic() - started < 1.0 and caught.value.full
        deadline = time.monotonic() + 2.0  # httpx's read timeout (0.3 s) ends the request thread
        while any(t.name == "rerank" for t in threading.enumerate()) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not any(t.name == "rerank" for t in threading.enumerate())
    finally:
        for conn in held:
            conn.close()
        server.close()


def test_no_budget_left_sends_no_request():
    calls: list[float] = []
    with pytest.raises(RerankTimeout) as caught:
        _dripping(timeout=3.0, calls=calls).score_within("q", ["a"], 0.0)
    assert calls == [] and not caught.value.full


def test_a_failing_reranker_is_reported_as_the_reranker_not_the_embedder(settings, client, ask, login):
    services = get_services()
    services.reranker = GuardedReranker(_dripping(timeout=0.3, interval=0.2, drops=20),
                                        CircuitBreaker(settings), "rerank:slow")
    try:
        _, events = ask(login("customer@leon.demo"), "退款多久到账")
    finally:
        services.reranker = None
    with new_session() as db:
        run = db.get(Run, events[0]["run_id"])
        assert run.error.startswith("rerank unavailable") and error_kind(run.error) == "rerank"


def test_a_dripping_reranker_no_longer_holds_an_answer_past_the_run_deadline(settings, client, ask, login):
    settings.run_deadline_seconds = 1.5
    calls: list[float] = []
    services = get_services()
    services.reranker = GuardedReranker(_dripping(timeout=3.0, interval=0.5, drops=20, calls=calls),
                                        CircuitBreaker(settings), "rerank:drip")
    started = time.monotonic()
    try:
        _, events = ask(login("customer@leon.demo"), "退款多久到账")
    finally:
        services.reranker = None
    elapsed = time.monotonic() - started
    final = by_type(events, "final")[0]
    assert elapsed < 4.0, f"{elapsed:.1f} s: before N-141 this waited out the drip twice (20 s)"
    assert len(calls) == 1, "the policy quote after the deadline does not call the reranker again"
    assert final["status"] == "completed" and "[" in final["text"], "the customer still gets the policy"
    with new_session() as db:
        assert db.get(Run, events[0]["run_id"]).error == "run deadline reached", "not an embedding outage"
