"""N-108: a model that keeps the connection alive but says nothing useful. httpx's read timeout
resets on every byte, so an upstream sending an empty delta every 8 s held one FAQ answer for 242 s
(run deadline 45 s, stall timeout 10 s) and never opened the breaker."""

from __future__ import annotations

import time

import httpx
import pytest

from app.llm.base import LLMDeadlineError, LLMMessage, LLMRetryableError
from app.llm.breaker import CircuitBreaker
from app.llm.errors import error_kind
from app.llm.openai_compat import OpenAICompatProvider
from tests.conftest import by_type
from tests.test_model_stall import silent_server  # noqa: F401  (fixture)

HEARTBEAT = b'data: {"choices":[{"index":0,"delta":{}}]}\n\n'


def _chunk(text: str) -> bytes:
    return ('data: {"choices":[{"index":0,"delta":{"content":"%s"}}]}\n\n' % text).encode()


class Drip(httpx.SyncByteStream):
    """Sends ``pieces`` one every ``every`` seconds, then (optionally) a proper ending."""

    def __init__(self, pieces, every: float, end: bool = False, fail: Exception | None = None):
        self.pieces, self.every, self.end, self.fail = pieces, every, end, fail
        self.closed = False

    def __iter__(self):
        for piece in self.pieces:
            time.sleep(self.every)
            if self.closed:
                return
            yield piece
        if self.fail:
            raise self.fail
        if self.end:
            yield b"data: [DONE]\n\n"

    def close(self):
        self.closed = True


def _provider(stream: httpx.SyncByteStream, **kwargs) -> OpenAICompatProvider:
    return OpenAICompatProvider("https://x/v1", "k", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})), **kwargs)


def _ask(provider, deadline: float | None = None):
    options = {"deadline": time.monotonic() + deadline} if deadline is not None else {}
    return provider.chat([LLMMessage(role="user", content="积分会过期吗")], [], "m", options)


def test_heartbeats_are_not_output_so_the_stall_timeout_fires():
    provider = _provider(Drip([HEARTBEAT] * 100, every=0.1), timeout=60.0, stall_timeout=0.5)
    started = time.monotonic()
    with pytest.raises(LLMRetryableError, match="stall timeout") as caught:
        _ask(provider, deadline=30)
    assert time.monotonic() - started < 1.2, "every heartbeat restarted the wait"
    assert error_kind(str(caught.value)) == "timeout"


def test_a_byte_every_tenth_of_a_second_stops_at_the_run_deadline():
    # Not even one full line arrives: only the caller's own clock can stop it.
    provider = _provider(Drip([b"d"] * 200, every=0.1), timeout=60.0, stall_timeout=10.0)
    started = time.monotonic()
    with pytest.raises(LLMDeadlineError) as caught:
        _ask(provider, deadline=0.8)
    assert time.monotonic() - started < 1.3
    assert error_kind(str(caught.value)) == "timeout"


def test_real_output_slower_than_the_call_timeout_is_cut_at_the_call_timeout():
    provider = _provider(Drip([_chunk("积")] * 100, every=0.1), timeout=1.0, stall_timeout=0.5)
    started = time.monotonic()
    with pytest.raises(LLMRetryableError, match="call timeout"):
        _ask(provider)
    assert time.monotonic() - started < 1.5


def test_a_slow_but_steady_answer_still_completes():
    provider = _provider(Drip([_chunk("积分"), HEARTBEAT, _chunk("一年"), _chunk("有效")], every=0.15, end=True),
                         timeout=60.0, stall_timeout=0.5)
    assert _ask(provider, deadline=30).content == "积分一年有效"


def test_a_thinking_model_reasoning_longer_than_the_stall_timeout_is_not_a_stall():
    thinking = b'data: {"choices":[{"index":0,"delta":{"reasoning_content":"\xe5\x85\x88"}}]}\n\n'
    provider = _provider(Drip([thinking] * 8 + [_chunk("一年")], every=0.1, end=True),
                         timeout=60.0, stall_timeout=0.5)
    assert _ask(provider, deadline=30).content == "一年"


def test_a_call_cut_by_a_nearly_spent_run_is_not_the_models_fault():
    provider = _provider(Drip([HEARTBEAT] * 100, every=0.05), timeout=60.0, stall_timeout=1.0)
    with pytest.raises(LLMDeadlineError) as short:
        _ask(provider, deadline=0.3)
    assert short.value.blameless, "0.3 s left of the run says nothing about the model"
    provider = _provider(Drip([_chunk("积")] * 100, every=0.1), timeout=60.0, stall_timeout=0.5)
    with pytest.raises(LLMDeadlineError) as fair:
        _ask(provider, deadline=0.8)
    assert not fair.value.blameless


def test_a_silent_model_cut_by_a_nearly_spent_run_is_blameless_too():
    # Review: the reader's own read timeout and the caller's clock raced, so a model sending nothing
    # was counted against the breaker 14 times in 15.
    for _ in range(5):
        provider = _provider(Drip([b""], every=3.0), timeout=60.0, stall_timeout=1.0)
        with pytest.raises(LLMDeadlineError) as caught:
            _ask(provider, deadline=0.3)
        assert caught.value.blameless


def test_a_model_that_never_answers_a_nearly_spent_run_is_blameless(silent_server):  # noqa: F811
    # A real socket that accepts and sends nothing: httpx's own read timeout fires, not the caller's.
    url, _, _ = silent_server
    for _ in range(3):
        provider = OpenAICompatProvider(url, "k", timeout=60.0, stall_timeout=1.0)
        with pytest.raises(LLMDeadlineError) as caught:
            _ask(provider, deadline=0.3)
        assert caught.value.blameless


def test_a_paused_consumer_is_not_a_stalled_model():
    # Review: lines that arrived while the caller was busy (a slow client) were reported as a stall.
    provider = _provider(Drip([_chunk("积")] * 30, every=0.02, end=True), timeout=60.0, stall_timeout=0.5)
    received = []
    for event in provider.stream([LLMMessage(role="user", content="q")], [], "m", {"deadline": time.monotonic() + 30}):
        received.append(event)
        if len(received) == 3:
            time.sleep(0.8)
    assert received[-1].type == "done" and received[-1].result.content == "积" * 30


def test_a_connection_reset_mid_stream_is_an_unavailable_model():
    provider = _provider(Drip([_chunk("积")], every=0.01, fail=httpx.ReadError("reset")), stall_timeout=1.0)
    with pytest.raises(LLMRetryableError, match="stream interrupted") as caught:
        _ask(provider, deadline=30)
    assert error_kind(str(caught.value)) == "unavailable"


def test_the_faq_answer_comes_back_near_the_run_deadline(settings, client, ask, login, monkeypatch):
    from app.services import get_services

    settings.run_deadline_seconds = 1.0
    provider = _provider(Drip([HEARTBEAT] * 400, every=0.05), timeout=60.0, stall_timeout=10.0)
    monkeypatch.setattr(get_services(), "provider", provider)
    started = time.monotonic()
    _, events = ask(login("customer@leon.demo"), "退款多久能到账？")
    elapsed = time.monotonic() - started
    final = by_type(events, "final")[0]
    assert elapsed < 3.0, f"the heartbeat stream held the answer for {elapsed:.1f} s"
    assert "以下是相关规定的原文" in final["text"] and final["meta"].get("degraded"), final["text"]


def test_the_fallback_tier_is_not_charged_for_a_call_never_made(settings, ask, login, monkeypatch):
    # Review: after the first tier ran out the run, the second was "called" past the deadline and
    # counted as failed, so a fallback model nobody had asked opened after three such runs.
    from app.services import get_services

    settings.run_deadline_seconds = 0.4
    settings.llm_stall_timeout = 1.0
    breaker = get_services().breaker
    for _ in range(4):
        provider = _provider(Drip([HEARTBEAT] * 100, every=0.05), timeout=60.0, stall_timeout=1.0)
        monkeypatch.setattr(get_services(), "provider", provider)
        ask(login("customer@leon.demo"), "退款多久能到账？")
    assert not breaker._failures and not breaker._opened, (breaker._failures, breaker._opened)


def test_a_blameless_cut_hands_back_the_half_open_trial(settings):
    # The breaker's half-open trial must not be burnt on a run that had no time left.
    from app.agent.runtime import AgentRuntime

    now = [1000.0]
    breaker = CircuitBreaker(settings, clock=lambda: now[0])
    settings.llm_breaker_failures, settings.llm_breaker_cooldown_seconds = 1, 10
    breaker.record("m", ok=False)
    now[0] = 1020.0  # cooled down: the next caller makes the half-open trial

    def cut(*a, **k):
        raise LLMDeadlineError("run deadline reached while the model was answering", blameless=True)
        yield

    runtime = type("Runtime", (), {"breaker": breaker, "provider": type("P", (), {"stream": staticmethod(cut)})()})()

    class Tracer:
        run = type("R", (), {"model": None})()

        def span(self, *a, **k):
            import contextlib
            return contextlib.nullcontext(type("S", (), {"set": lambda *a, **k: None})())

    with pytest.raises(LLMDeadlineError):
        list(AgentRuntime._call_llm(runtime, Tracer(), [], [], {"models": ["m"], "options": {}, "steps": 1}))
    assert breaker.state("m") == "open" and breaker.allow("m"), "the trial was handed back, not counted"
