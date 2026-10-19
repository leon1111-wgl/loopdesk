import json

import httpx
import pytest

from app.channels.feishu import FeishuClient
from app.channels.wecom import notify_approval
from app.config import Settings
from app.llm.anthropic_provider import AnthropicProvider, to_anthropic
from app.llm.base import LLMError, LLMMessage, ToolCall, ToolSpec
from app.llm.openai_compat import OpenAICompatProvider
from app.llm.router import ModelRouter
from app.models import Approval, Tenant

TOOLS = [ToolSpec("get_order", "查询订单", {"type": "object", "properties": {"order_id": {"type": "string"}}})]


def sse(*chunks: dict) -> bytes:
    return "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n"


def test_openai_compat_reassembles_streamed_tool_call_fragments():
    body = sse(
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                            "function": {"name": "get_order", "arguments": "{\"order"}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "_id\": \"SO20261008002\"}"}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 18}},
    )
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    provider = OpenAICompatProvider("https://ark.example/api/v3", "test-key", transport=httpx.MockTransport(handler))
    result = provider.chat([LLMMessage(role="user", content="查订单")], TOOLS, "doubao-test")
    assert result.tool_calls == [ToolCall("call_1", "get_order", {"order_id": "SO20261008002"})]
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (120, 18)
    assert seen["tools"][0]["function"]["name"] == "get_order" and seen["stream"] is True


def test_openai_compat_streams_text_and_estimates_usage_when_missing():
    body = sse({"choices": [{"delta": {"content": "退款"}}]}, {"choices": [{"delta": {"content": "已受理"}, "finish_reason": "stop"}]})
    provider = OpenAICompatProvider("https://x/v1", "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)))
    events = list(provider.stream([LLMMessage(role="user", content="hi")], [], "m"))
    assert [e.text for e in events if e.type == "delta"] == ["退款", "已受理"]
    done = events[-1].result
    assert done.content == "退款已受理" and done.usage.estimated


def test_openai_compat_retries_then_raises_fatal_errors_without_retry():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    provider = OpenAICompatProvider("https://x/v1", "k", transport=httpx.MockTransport(flaky))
    provider_sleep = __import__("app.llm.openai_compat", fromlist=["time"]).time
    original = provider_sleep.sleep
    provider_sleep.sleep = lambda s: None
    try:
        assert provider.chat([LLMMessage(role="user", content="hi")], [], "m").content == "ok"
        assert calls["n"] == 2
        bad = OpenAICompatProvider("https://x/v1", "k", transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "bad"})))
        with pytest.raises(LLMError):
            bad.chat([LLMMessage(role="user", content="hi")], [], "m")
    finally:
        provider_sleep.sleep = original


def test_anthropic_conversion_groups_tool_results_and_replays_raw_blocks():
    raw_blocks = [{"type": "thinking", "thinking": "", "signature": "sig"},
                  {"type": "tool_use", "id": "toolu_1", "name": "get_order", "input": {"order_id": "SO1"}},
                  {"type": "tool_use", "id": "toolu_2", "name": "track_shipment", "input": {"order_id": "SO1"}}]
    system, messages = to_anthropic([
        LLMMessage(role="system", content="你是客服"),
        LLMMessage(role="user", content="查订单"),
        LLMMessage(role="assistant", tool_calls=[ToolCall("toolu_1", "get_order", {}), ToolCall("toolu_2", "track_shipment", {})], raw=raw_blocks),
        LLMMessage(role="tool", content="{}", tool_call_id="toolu_1", name="get_order"),
        LLMMessage(role="tool", content="{\"error\": \"x\"}", tool_call_id="toolu_2", name="track_shipment", is_error=True),
    ])
    assert system == "你是客服"
    assert messages[1] == {"role": "assistant", "content": raw_blocks}, "thinking blocks replayed unchanged"
    assert messages[2]["role"] == "user" and len(messages[2]["content"]) == 2, "one user turn for all tool results"
    assert messages[2]["content"][1]["is_error"] is True


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class FakeClient:
    def __init__(self, response):
        self.beta = type("Beta", (), {"messages": FakeMessages(response)})()


def test_anthropic_provider_maps_tool_use_and_request_shape():
    response = {"model": "claude-opus-5-5", "stop_reason": "tool_use", "usage": {"input_tokens": 50, "output_tokens": 9},
                "content": [{"type": "text", "text": "我查一下。"},
                            {"type": "tool_use", "id": "toolu_9", "name": "get_order", "input": {"order_id": "SO1"}}]}
    client = FakeClient(response)
    result = AnthropicProvider(None, client=client).chat(
        [LLMMessage(role="system", content="sys"), LLMMessage(role="user", content="hi")], TOOLS, "claude-opus-5-5")
    assert result.finish_reason == "tool_calls" and result.tool_calls[0].arguments == {"order_id": "SO1"}
    sent = client.beta.messages.kwargs
    assert sent["system"] == "sys" and sent["tools"][0]["input_schema"]["type"] == "object"
    assert "temperature" not in sent and sent["output_config"] == {"effort": "low"}
    assert sent["fallbacks"] == "default"


def test_anthropic_refusal_becomes_empty_answer():
    response = {"model": "claude-opus-5-5", "stop_reason": "refusal", "usage": {}, "content": [{"type": "text", "text": "partial"}]}
    result = AnthropicProvider(None, client=FakeClient(response)).chat([LLMMessage(role="user", content="x")], [], "m")
    assert result.finish_reason == "refusal" and result.content == ""


def test_router_sends_high_stakes_to_strong_model():
    router = ModelRouter(Settings())
    assert router.route("积分怎么用").tier == "fast"
    assert router.route("退款多久能到账？").tier == "fast", "a policy question is not a money action"
    assert router.route("SO20261008002 我要退款").tier == "strong"
    decision = router.route("订单 SO1 我要退款，不退我就投诉到12315")
    assert decision.tier == "strong" and "高风险意图" in decision.reasons
    assert router.candidates(decision) == ["mock-strong", "mock-fast"]


def test_wecom_notification_dry_run_and_live_payload():
    approval = Approval(id=7, tool_name="request_refund", arguments={"order_id": "SO1", "amount": 1299.0}, reason="超额")
    tenant = Tenant(name="Leon优选")
    assert notify_approval(Settings(), approval, tenant)["mode"] == "dry_run"
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        assert request.url.params["key"] == "robot-key"
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    s = Settings()
    s.wecom_webhook_key = "robot-key"
    assert notify_approval(s, approval, tenant, transport=httpx.MockTransport(handler))["sent"]
    assert sent["msgtype"] == "markdown" and "¥1299.00" in sent["markdown"]["content"]


def test_feishu_url_verification_and_token_check(settings, client):
    settings.feishu_verification_token = "verify-me"
    ok = client.post("/api/channels/feishu/leon/events",
                     json={"type": "url_verification", "challenge": "abc", "token": "verify-me"})
    assert ok.json() == {"challenge": "abc"}
    bad = client.post("/api/channels/feishu/leon/events",
                      json={"type": "url_verification", "challenge": "abc", "token": "wrong"})
    assert bad.status_code == 403


def test_feishu_message_event_runs_agent_once(client, monkeypatch):
    replies = []
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: replies.append((mid, text)) or {"sent": False})
    event = {
        "schema": "2.0",
        "header": {"event_id": "evt-001", "event_type": "im.message.receive_v1", "token": ""},
        "event": {"sender": {"sender_id": {"open_id": "ou_abc123"}},
                  "message": {"message_id": "om_1", "chat_id": "oc_1", "message_type": "text",
                              "content": json.dumps({"text": "@_user_1 退款多久能到账？"})}},
    }
    assert client.post("/api/channels/feishu/leon/events", json=event).json() == {"code": 0}
    assert client.post("/api/channels/feishu/leon/events", json=event).json()["msg"] == "duplicate"
    assert len(replies) == 1 and replies[0][0] == "om_1"
    assert "1-3个工作日" in replies[0][1]


def _feishu_event(event_id: str, text: str = "退款多久能到账？") -> dict:
    return {
        "schema": "2.0",
        "header": {"event_id": event_id, "event_type": "im.message.receive_v1", "token": ""},
        "event": {"sender": {"sender_id": {"open_id": "ou_abc123"}},
                  "message": {"message_id": f"om_{event_id}", "chat_id": "oc_1", "message_type": "text",
                              "content": json.dumps({"text": text})}},
    }


def test_feishu_redelivery_to_another_worker_is_still_a_duplicate(client, monkeypatch):
    # With several workers (or after a restart) the re-sent copy reaches a process that did
    # not receive the original. Each process has its own deduper object; the IDs they
    # check are shared, so the customer is answered once.
    from app.api import channels
    from app.channels.feishu import EventDeduper

    replies = []
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: replies.append((mid, text)) or {"sent": False})
    assert client.post("/api/channels/feishu/leon/events", json=_feishu_event("evt-retry")).json() == {"code": 0}
    monkeypatch.setattr(channels, "deduper", EventDeduper())  # the worker that gets the retry
    assert client.post("/api/channels/feishu/leon/events", json=_feishu_event("evt-retry")).json()["msg"] == "duplicate"
    assert len(replies) == 1


def test_feishu_concurrent_deliveries_let_exactly_one_through(client):
    import threading

    from app.channels.feishu import EventDeduper
    from app.db import new_session

    barrier, results = threading.Barrier(8, timeout=10), []

    def deliver():
        with new_session() as db:
            barrier.wait()
            results.append(EventDeduper().first_time(db, "feishu", "evt-race"))

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False] * 7 + [True]


def test_feishu_event_ids_expire_after_a_day(client):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.channels.feishu import EventDeduper
    from app.db import new_session
    from app.models import ChannelEvent

    two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
    with new_session() as db:
        db.add(ChannelEvent(channel="feishu", event_id="evt-old", received_at=two_days_ago, status="done"))
        db.add(ChannelEvent(channel="feishu", event_id="evt-unanswered", received_at=two_days_ago, status="received"))
        db.commit()
        assert EventDeduper().first_time(db, "feishu", "evt-new")
        assert sorted(db.execute(select(ChannelEvent.event_id)).scalars()) == ["evt-new", "evt-unanswered"], \
            "an answered event expires; one nobody answered is still work to do"


def _stored_events():
    from sqlalchemy import select

    from app.db import new_session
    from app.models import ChannelEvent

    with new_session() as db:
        return {e.event_id: (e.status, e.attempts) for e in db.execute(select(ChannelEvent)).scalars()}


def _accept_without_answering(event_id: str, text: str = "退款多久能到账？") -> None:
    # The process died right after Feishu got its acknowledgement: stored, never answered.
    from app.api.channels import _accept
    from app.channels.feishu import parse_text_event

    parsed = parse_text_event(_feishu_event(event_id, text))
    assert _accept(event_id, {**parsed, "tenant": "leon"})


def test_an_answered_event_is_settled(client, monkeypatch):
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: {"sent": False})
    client.post("/api/channels/feishu/leon/events", json=_feishu_event("evt-ok"))
    assert _stored_events()["evt-ok"] == ("done", 1)


def test_a_message_whose_worker_died_after_the_acknowledgement_is_still_answered(client, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.channels.feishu import retry_stuck
    from app.services import get_services

    replies = []
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: replies.append((mid, text)) or {"sent": False})
    _accept_without_answering("evt-lost")
    assert retry_stuck(get_services()) == {"answered": 0, "gave_up": 0}, "its own task may still be starting"
    later = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert retry_stuck(get_services(), now=later) == {"answered": 1, "gave_up": 0}
    assert [mid for mid, _ in replies] == ["om_evt-lost"] and "1-3个工作日" in replies[0][1]
    assert _stored_events()["evt-lost"] == ("done", 1)
    assert retry_stuck(get_services(), now=later + timedelta(hours=1)) == {"answered": 0, "gave_up": 0}


def test_a_claim_is_taken_over_only_when_its_worker_is_presumed_dead(client, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.channels.feishu import process_event, retry_stuck
    from app.db import new_session
    from app.services import get_services

    replies = []
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: replies.append(mid) or {"sent": False})
    _accept_without_answering("evt-slow")
    now = datetime.now(timezone.utc)
    with new_session() as db:  # a worker claimed it and is still running
        from app.channels.feishu import _claim
        assert _claim(db, "evt-slow", now, now - timedelta(minutes=5)) is not None
    assert process_event(get_services(), "evt-slow", now) is None, "the event's own task does not run it twice"
    assert retry_stuck(get_services(), now=now + timedelta(minutes=2)) == {"answered": 0, "gave_up": 0}
    assert replies == []
    assert retry_stuck(get_services(), now=now + timedelta(minutes=10)) == {"answered": 1, "gave_up": 0}
    assert replies == ["om_evt-slow"] and _stored_events()["evt-slow"] == ("done", 2)


def test_after_repeated_crashes_the_customer_gets_the_fallback_once(client, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from app.agent.runtime import MODEL_ERROR_TEXT
    from app.channels.feishu import MAX_ATTEMPTS, retry_stuck
    from app.db import new_session
    from app.models import ChannelEvent
    from app.services import get_services

    replies = []
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: replies.append((mid, text)) or {"sent": False})
    _accept_without_answering("evt-poison")
    long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    with new_session() as db:  # every worker that took it died
        db.execute(update(ChannelEvent).values(status="processing", attempts=MAX_ATTEMPTS, claimed_at=long_ago))
        db.commit()
    assert retry_stuck(get_services()) == {"answered": 0, "gave_up": 1}
    assert retry_stuck(get_services()) == {"answered": 0, "gave_up": 0}
    assert replies == [("om_evt-poison", MODEL_ERROR_TEXT)]
    assert _stored_events()["evt-poison"] == ("failed", MAX_ATTEMPTS)


def test_concurrent_claims_answer_an_event_once(client, monkeypatch):
    import threading

    from app.channels.feishu import process_event
    from app.services import get_services

    replies = []
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: replies.append(mid) or {"sent": False})
    _accept_without_answering("evt-race2")
    barrier = threading.Barrier(4, timeout=10)

    def worker():
        barrier.wait()
        process_event(get_services(), "evt-race2")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert replies == ["om_evt-race2"]


def test_a_new_customers_first_messages_answered_at_once_share_one_user_and_one_conversation(client, monkeypatch):
    # Two workers answer a new Feishu customer's first two messages at the same time. Both found
    # no user and no conversation; one insert failed (fallback reply), or two conversations were
    # created and every later message of that chat failed on an ambiguous lookup.
    import threading

    from sqlalchemy import func, select

    from app.agent.runtime import MODEL_ERROR_TEXT
    from app.channels.feishu import process_event
    from app.db import new_session
    from app.models import Conversation, User
    from app.services import get_services

    replies = []
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: replies.append(text) or {"sent": False})
    for event_id in ("evt-first-a", "evt-first-b"):
        _accept_without_answering(event_id)
    barrier = threading.Barrier(2, timeout=10)

    def worker(event_id):
        barrier.wait()
        process_event(get_services(), event_id)

    threads = [threading.Thread(target=worker, args=(e,)) for e in ("evt-first-a", "evt-first-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(replies) == 2 and MODEL_ERROR_TEXT not in replies
    with new_session() as db:
        assert db.execute(select(func.count(User.id)).where(User.email.like("feishu_%"))).scalar_one() == 1
        assert db.execute(select(func.count(Conversation.id)).where(Conversation.channel == "feishu")).scalar_one() == 1


def test_feishu_event_without_an_id_is_ignored(client, monkeypatch):
    replies = []
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: replies.append((mid, text)) or {"sent": False})
    event = _feishu_event("evt-x")
    del event["header"]["event_id"]
    assert client.post("/api/channels/feishu/leon/events", json=event).json()["msg"] == "ignored"
    assert replies == []


def test_feishu_background_failure_is_logged_and_answered(client, monkeypatch, caplog):
    import logging

    from app.agent.runtime import MODEL_ERROR_TEXT
    from app.services import get_services

    replies = []
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: replies.append((mid, text)) or {"sent": False})

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(get_services().provider, "stream", broken)
    event = {
        "schema": "2.0",
        "header": {"event_id": "evt-fail", "event_type": "im.message.receive_v1", "token": ""},
        "event": {"sender": {"sender_id": {"open_id": "ou_def456"}},
                  "message": {"message_id": "om_9", "chat_id": "oc_9", "message_type": "text",
                              "content": json.dumps({"text": "积分可以怎么抵扣？"})}},
    }
    with caplog.at_level(logging.ERROR, logger="app.channels.feishu"):
        assert client.post("/api/channels/feishu/leon/events", json=event).json() == {"code": 0}
    assert replies == [("om_9", MODEL_ERROR_TEXT)], "the customer still gets an answer"
    assert "om_9" in caplog.text and "database is locked" in caplog.text
    assert _stored_events()["evt-fail"] == ("failed", 1), "answered with the fallback: not retried"


def test_mcp_server_tools_respect_permissions(settings, client, monkeypatch):
    from app import mcp_server
    from app.agent.business_tools import get_order
    from app.services import get_services

    monkeypatch.setenv("LOOPDESK_MCP_USER", "customer@leon.demo")
    out = json.loads(mcp_server._call(get_services(), get_order, {"order_id": "SO20261003005"}))
    assert out == {"error": "订单不存在或无权查看"}
    monkeypatch.setenv("LOOPDESK_MCP_USER", "agent@leon.demo")
    assert json.loads(mcp_server._call(get_services(), get_order, {"order_id": "SO20261003005"}))["amount"] == 899.0


def test_openai_compat_never_retries_past_the_run_deadline():
    import time as clock

    from app.llm.base import LLMMessage, LLMRetryableError

    calls = {"n": 0}

    def overloaded(request):
        calls["n"] += 1
        return httpx.Response(503, json={"error": "overloaded"})

    provider = OpenAICompatProvider("https://x/v1", "k", transport=httpx.MockTransport(overloaded))
    started = clock.monotonic()
    with pytest.raises(LLMRetryableError):
        provider.chat([LLMMessage(role="user", content="hi")], [], "m", {"deadline": started + 0.5})
    assert clock.monotonic() - started < 1.0, "the 1 s and 2 s back-offs would run past the deadline"
    assert calls["n"] == 1


def test_overlapping_sweepers_do_not_answer_the_same_message_twice(client, monkeypatch):
    # Code review: a sweep stamped every claim with the time the sweep started. One that spent
    # five minutes answering a backlog had back-dated its latest claim, and the next cron run
    # took that event over as stale while it was still being answered.
    from datetime import datetime, timedelta, timezone

    from app.channels import feishu
    from app.services import get_services

    replies = []
    monkeypatch.setattr(FeishuClient, "reply", lambda self, mid, text: replies.append(mid) or {"sent": False})
    for event_id in ("evt-b1", "evt-b2"):
        _accept_without_answering(event_id)
    start = datetime.now(timezone.utc) + timedelta(minutes=1)
    clock = iter([start, start + timedelta(minutes=5)])  # the first sweep reaches evt-b2 five minutes in
    monkeypatch.setattr(feishu, "_utcnow", lambda: next(clock))
    answer, overlapping = feishu.handle_message, []

    def slow_answer(services, tenant_slug, parsed):
        if parsed["event_id"] == "evt-b2":  # cron starts the next sweep meanwhile
            overlapping.append(feishu.retry_stuck(services, now=start + timedelta(minutes=5, seconds=1)))
        return answer(services, tenant_slug, parsed)

    monkeypatch.setattr(feishu, "handle_message", slow_answer)
    assert feishu.retry_stuck(get_services(), now=start) == {"answered": 2, "gave_up": 0}
    assert overlapping == [{"answered": 0, "gave_up": 0}]
    assert replies == ["om_evt-b1", "om_evt-b2"]
