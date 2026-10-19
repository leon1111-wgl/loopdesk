import json

import httpx
import pytest

from app.channels.feishu import FeishuClient, deduper
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
    deduper._seen.clear()
    assert client.post("/api/channels/feishu/leon/events", json=event).json() == {"code": 0}
    assert client.post("/api/channels/feishu/leon/events", json=event).json()["msg"] == "duplicate"
    assert len(replies) == 1 and replies[0][0] == "om_1"
    assert "1-3个工作日" in replies[0][1]


def test_mcp_server_tools_respect_permissions(settings, client, monkeypatch):
    from app import mcp_server
    from app.agent.business_tools import get_order
    from app.services import get_services

    monkeypatch.setenv("LOOPDESK_MCP_USER", "customer@leon.demo")
    out = json.loads(mcp_server._call(get_services(), get_order, {"order_id": "SO20261003005"}))
    assert out == {"error": "订单不存在或无权查看"}
    monkeypatch.setenv("LOOPDESK_MCP_USER", "agent@leon.demo")
    assert json.loads(mcp_server._call(get_services(), get_order, {"order_id": "SO20261003005"}))["amount"] == 899.0
