"""N-166: a stream that stops before the model says it is done. An upstream that closed the
connection after "您的退款将在" (no finish_reason, no [DONE]) gave the customer those five words as
the answer, and finish_reason=length did the same with "退款审核通过后，微信支付预计"; both runs were
recorded as completed with no error."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from app.llm.base import LLMMessage, LLMRetryableError
from app.llm.errors import HINTS, error_kind
from app.llm.openai_compat import OpenAICompatProvider
from tests.conftest import by_type


def _content(text: str, finish: str | None = None) -> str:
    choice = {"index": 0, "delta": {"content": text}, "finish_reason": finish}
    return "data: " + json.dumps({"choices": [choice]}, ensure_ascii=False) + "\n\n"


def _tool_call(arguments: str, finish: str | None = None) -> str:
    call = {"index": 0, "id": "c1", "function": {"name": "lookup_order", "arguments": arguments}}
    choice = {"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": finish}
    return "data: " + json.dumps({"choices": [choice]}, ensure_ascii=False) + "\n\n"


DONE = "data: [DONE]\n\n"


class Upstream:
    """Answers each request with the next body in ``bodies`` (the last one repeats)."""

    def __init__(self, *bodies: str):
        self.bodies, self.calls = list(bodies), 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = self.bodies[min(self.calls, len(self.bodies) - 1)]
        self.calls += 1
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def _provider(upstream: Upstream) -> OpenAICompatProvider:
    return OpenAICompatProvider("https://x/v1", "k", transport=httpx.MockTransport(upstream), stall_timeout=2.0)


def _ask(provider):
    return provider.chat([LLMMessage(role="user", content="退款一般几天能到")], [], "m",
                         {"deadline": time.monotonic() + 30})


# ---------------------------------------------------------------- the adapter

def test_a_clean_close_without_an_ending_is_a_broken_stream():
    with pytest.raises(LLMRetryableError, match="stream interrupted") as caught:
        _ask(_provider(Upstream(_content("您的退款将在"))))
    assert error_kind(str(caught.value)) == "unavailable"


def test_a_tool_call_cut_off_by_a_close_is_not_run():
    # The arguments stop mid-JSON: before, the call reached the tools as {"__invalid_json__": ...}.
    with pytest.raises(LLMRetryableError, match="stream interrupted"):
        _ask(_provider(Upstream(_tool_call('{"order_id": "SO2026'))))


@pytest.mark.parametrize("body", [
    _content("1-3个工作日", finish="stop"),          # some services never send [DONE]
    _content("1-3个工作日") + DONE,                  # and some never send a finish_reason
    _content("1-3个工作日") + _content("", finish="stop") + DONE,
], ids=["finish-only", "done-only", "both"])
def test_either_ending_marker_is_a_finished_answer(body):
    result = _ask(_provider(Upstream(body)))
    assert result.content == "1-3个工作日" and result.finish_reason == "stop"


def test_length_stays_length_when_the_cut_falls_inside_a_tool_call():
    result = _ask(_provider(Upstream(_tool_call('{"order_id": "SO20', finish="length") + DONE)))
    assert result.finish_reason == "length", "a call with half its arguments must not run as a tool call"


def test_truncation_has_its_own_error_kind_and_hint():
    assert error_kind("truncated: finish_reason=length, the model hit its output limit") == "truncated"
    assert "max_tokens" in HINTS["truncated"]


# ---------------------------------------------------------------- what the customer gets

def _use(monkeypatch, upstream: Upstream) -> None:
    from app.services import get_services

    monkeypatch.setattr(get_services(), "provider", _provider(upstream))


def _run(client, login, events) -> dict:
    run_id = by_type(events, "done")[0]["run_id"]
    return client.get(f"/api/runs/{run_id}", headers=login("admin@leon.demo")).json()


def test_a_closed_stream_is_answered_like_an_outage(client, ask, login, monkeypatch):
    upstream = Upstream(_content("您的退款将在"))
    _use(monkeypatch, upstream)
    _, events = ask(login("customer@leon.demo"), "退款一般几天能到")
    final = by_type(events, "final")[0]
    # Broken on the first step, before any search: nothing to quote, so a person takes over.
    assert "您的退款将在" not in final["text"] and "转接人工客服" in final["text"], final["text"]
    run = _run(client, login, events)
    assert run["status"] == "handoff" and "stream interrupted" in (run["error"] or ""), run
    assert upstream.calls == 1, "tokens already reached the customer: the other tier must not add to them"


def test_a_tier_that_closes_before_saying_anything_falls_through_to_the_next(client, ask, login, monkeypatch):
    upstream = Upstream(_tool_call('{"order_id": "SO2026'), _content("退款审核通过后1-3个工作日到账。", finish="stop") + DONE)
    _use(monkeypatch, upstream)
    _, events = ask(login("customer@leon.demo"), "退款一般几天能到")
    assert upstream.calls == 2
    assert not by_type(events, "tool_call"), "the half call was never run"
    assert by_type(events, "final")[0]["text"].startswith("退款审核通过后1-3个工作日到账")


def test_an_answer_cut_at_the_output_limit_is_not_sent(client, ask, login, monkeypatch):
    _use(monkeypatch, Upstream(_content("退款审核通过后，微信支付预计", finish="length") + DONE))
    _, events = ask(login("customer@leon.demo"), "退款到账要多久")
    final = by_type(events, "final")[0]
    assert "微信支付预计" not in final["text"].split("以下是相关规定的原文")[0], final["text"]
    assert final["meta"].get("degraded")
    shown = "".join(e["text"] for e in by_type(events, "token"))
    assert not shown.endswith("预计"), "the gate's held-back tail is not flushed after a cut"
    trace = _run(client, login, events)
    assert (trace["error"] or "").startswith("truncated"), {k: trace.get(k) for k in ("status", "error")}
    assert any(s["name"] == "output_truncated" for s in trace["spans"])


def _reasoning_only(finish: str) -> str:
    choice = {"index": 0, "delta": {"reasoning_content": "先想想退款规则……"}, "finish_reason": finish}
    return "data: " + json.dumps({"choices": [choice]}, ensure_ascii=False) + "\n\n" + DONE


def test_a_cut_before_any_output_tries_the_next_tier(client, ask, login, monkeypatch):
    # Review P2: a reasoning model that spends its whole budget thinking shows the customer nothing,
    # so the other tier is still worth asking.
    upstream = Upstream(_reasoning_only("length"), _content("退款审核通过后1-3个工作日到账。", finish="stop") + DONE)
    _use(monkeypatch, upstream)
    _, events = ask(login("customer@leon.demo"), "退款一般几天能到")
    assert upstream.calls == 2
    assert by_type(events, "final")[0]["text"].startswith("退款审核通过后1-3个工作日到账")


def test_both_tiers_cut_before_any_output_is_a_truncated_run(client, ask, login, monkeypatch):
    from app.services import get_services

    _use(monkeypatch, Upstream(_reasoning_only("length")))
    _, events = ask(login("customer@leon.demo"), "退款到账要多久")
    trace = _run(client, login, events)
    assert (trace["error"] or "").startswith("truncated"), trace.get("error")
    assert not get_services().breaker._failures, "an output limit is configuration, not an unhealthy model"


def test_an_error_reported_in_the_stream_keeps_its_message():
    body = _content("您的") + 'data: {"error": {"message": "HunyuanError: 请求频率超限"}}\n\n'
    with pytest.raises(LLMRetryableError, match="请求频率超限"):
        _ask(_provider(Upstream(body)))


def test_a_content_filter_stop_is_not_sent_as_the_answer(client, ask, login, monkeypatch):
    _use(monkeypatch, Upstream(_content("关于这个问题，", finish="content_filter") + DONE))
    _, events = ask(login("customer@leon.demo"), "退款到账要多久")
    final = by_type(events, "final")[0]["text"]
    assert final != "关于这个问题，" and "人工" in final, final
