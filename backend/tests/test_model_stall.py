"""N-077: a model that hangs. Before, the adapter waited 60 s per attempt and retried, so every
customer in flight waited out the 45 s run deadline and was handed to a person, FAQ questions too."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
from sqlalchemy import func, select

from app.db import new_session
from app.llm.base import ChatResult, LLMMessage, LLMRetryableError, StreamEvent, ToolCall, Usage
from app.llm.errors import error_kind
from app.llm.openai_compat import OpenAICompatProvider
from app.models import Run, Ticket
from tests.conftest import by_type


@pytest.fixture
def silent_server():
    """Accepts connections and reads the request, then sends nothing: a hung upstream."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    accepted: list[socket.socket] = []
    stop, connected = threading.Event(), threading.Event()

    def serve():
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            accepted.append(conn)
            connected.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}/v1", accepted, connected
    stop.set()
    thread.join()
    for conn in accepted:
        conn.close()
    server.close()


def test_a_silent_model_fails_after_the_stall_timeout_without_retrying(silent_server):
    url, accepted, connected = silent_server
    provider = OpenAICompatProvider(url, "k", timeout=60.0, stall_timeout=0.3)
    started = time.monotonic()
    with pytest.raises(LLMRetryableError) as caught:
        provider.chat([LLMMessage(role="user", content="积分会过期吗")], [], "m",
                      {"deadline": time.monotonic() + 45})
    assert time.monotonic() - started < 2.0, "waited for the 60 s read timeout or retried"
    connected.wait(1.0)
    time.sleep(0.3)  # a retry would have connected again by now
    assert len(accepted) == 1, "a hanging service is not asked again"
    assert error_kind(str(caught.value)) == "timeout"


def test_a_stream_that_stops_midway_is_a_stall_too():
    class Stalling(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"\xe7\xa7\xaf"}}]}\n\n'
            raise httpx.ReadTimeout("no data")

    provider = OpenAICompatProvider("https://x/v1", "k", stall_timeout=0.3,
                                    transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stalling())))
    with pytest.raises(LLMRetryableError, match="stall timeout"):
        list(provider.stream([LLMMessage(role="user", content="hi")], [], "m"))


def test_an_error_status_whose_body_hangs_is_still_a_model_error():
    # Review: the error body was read outside the try, so a proxy that sent "503" and then stalled
    # raised a bare httpx.ReadTimeout and the customer got no degraded answer at all.
    class Hanging(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadTimeout("no body")
            yield b""

    provider = OpenAICompatProvider("https://x/v1", "k", stall_timeout=0.3, max_retries=0,
                                    transport=httpx.MockTransport(lambda r: httpx.Response(503, stream=Hanging())))
    with pytest.raises(LLMRetryableError, match="HTTP 503"):
        provider.chat([LLMMessage(role="user", content="hi")], [], "m")


def test_the_stall_timeout_never_exceeds_the_call_timeout():
    assert OpenAICompatProvider("https://x/v1", "k", timeout=5.0, stall_timeout=10.0).stall_timeout == 5.0
    assert OpenAICompatProvider("https://x/v1", "k", timeout=60.0).stall_timeout == 60.0  # 0: off


def _hanging(seconds: float, then_call: ToolCall | None = None):
    def stream(messages, tools, model, options=None):
        time.sleep(seconds)
        if then_call is None or any(m.role == "tool" for m in messages):
            raise LLMRetryableError("stall timeout: the model sent nothing for 10 s")
        yield StreamEvent(type="done", result=ChatResult(content="", tool_calls=[then_call], usage=Usage(),
                                                          model=model, finish_reason="tool_calls"))
    return stream


def _tickets(conversation_id: int) -> int:
    with new_session() as db:
        return db.execute(select(func.count(Ticket.id)).where(Ticket.conversation_id == conversation_id)).scalar_one()


def test_a_faq_question_past_the_deadline_gets_the_policy_quoted_not_a_ticket(settings, client, ask, login,
                                                                                monkeypatch):
    from app.services import get_services

    settings.run_deadline_seconds = 0.2
    settings.llm_breaker_failures = 0
    monkeypatch.setattr(get_services().provider, "stream", _hanging(0.25))
    conversation_id, events = ask(login("customer@leon.demo"), "退款多久能到账？")
    final = by_type(events, "final")[0]
    assert final["status"] == "completed", final["text"]
    assert "以下是相关规定的原文" in final["text"] and final["meta"].get("degraded")
    assert final["meta"].get("citations"), final["meta"]
    assert _tickets(conversation_id) == 0, "nobody needs to answer a quoted FAQ"
    trace = client.get(f"/api/runs/{events[0]['run_id']}", headers=login("admin@leon.demo")).json()
    assert any(s["name"] == "run_deadline" for s in trace["spans"])
    with new_session() as db:
        run = db.get(Run, events[0]["run_id"])
        assert error_kind(run.error) == "timeout" and run.status == "completed"


def test_past_the_deadline_after_a_tool_changed_something_a_person_takes_over(settings, ask, login, monkeypatch):
    # A quote would not tell the customer that a ticket was opened (or money moved) in this run.
    from app.services import get_services

    settings.run_deadline_seconds = 0.2
    call = ToolCall(id="t1", name="create_ticket", arguments={"title": "耳机没声音", "description": "积分会过期吗"})
    monkeypatch.setattr(get_services().provider, "stream", _hanging(0.25, then_call=call))
    conversation_id, events = ask(login("customer@leon.demo"), "积分会过期吗？")
    final = by_type(events, "final")[0]
    assert final["status"] == "handoff" and "处理时间过长" in final["text"]


def _hanging_after_approval(seconds: float):
    def stream(messages, tools, model, options=None):
        time.sleep(seconds)
        raise LLMRetryableError("stall timeout: the model sent nothing for 10 s")
        yield
    return stream


def _pending_refund_without_order_id(ask, login) -> tuple[int, int]:
    """A refund waiting for approval, as if the customer had named the product, not the order
    (N-057's list_my_orders path): the question itself has no order id to rule out quoting."""
    conversation_id, events = ask(login("customer@leon.demo"), "SO20261008002 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    with new_session() as db:
        run = db.get(Run, events[0]["run_id"])
        run.state = {**run.state, "user_text": "我买的那个要退款", "question": "我买的那个要退款"}
        db.commit()
    return conversation_id, approval_id


@pytest.mark.parametrize("decision, status, says", [("approve", "completed", "已受理"),
                                                    ("reject", "handoff", "处理时间过长")])
def test_past_the_deadline_after_an_approval_the_customer_hears_the_decision(
        decision, status, says, settings, client, ask, login, monkeypatch):
    # Review: the deadline path quoted the refund policy to a customer whose refund had just been
    # filed (or rejected), where the model-down path says what was done.
    from app.services import get_services

    _, approval_id = _pending_refund_without_order_id(ask, login)
    settings.run_deadline_seconds = 0.2
    settings.llm_breaker_failures = 0
    monkeypatch.setattr(get_services().provider, "stream", _hanging_after_approval(0.25))
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": decision, "comment": "核对"},
                    headers=login("admin@leon.demo"))
    final = r.json()["final"]
    assert final["status"] == status and says in final["text"], final
    assert "以下是相关规定的原文" not in final["text"]


def test_tools_running_out_the_clock_still_count_as_a_failure(settings, ask, login, monkeypatch):
    # Review: past the deadline with no model error, the quoted answer was "completed" with no error,
    # so the dashboard counted it as resolved by self-service.
    from app.agent import runtime as agent_runtime

    settings.run_deadline_seconds = 0.2
    real = agent_runtime.AgentRuntime._run_tool

    def slow(self, tracer, ctx, call):
        if call.id != "degraded_search":
            time.sleep(0.25)
        return real(self, tracer, ctx, call)

    monkeypatch.setattr(agent_runtime.AgentRuntime, "_run_tool", slow)
    _, events = ask(login("customer@leon.demo"), "退款多久能到账？")
    assert by_type(events, "final")[0]["status"] == "completed"
    with new_session() as db:
        run = db.get(Run, events[0]["run_id"])
        assert run.error == "run deadline reached" and error_kind(run.error) == "timeout"
