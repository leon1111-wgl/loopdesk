"""The AI disclosure spoken at call start, Traditional Chinese for tenants whose customers read it, and the
keypad fallback for order numbers and amounts. Fake speech engines, no models."""

import json
import threading

import numpy as np
import pytest
from sqlalchemy import select

from app.db import new_session
from app.governance import disclosure, script
from app.models import Message, Tenant
from app.voice.endpoint import SAMPLE_RATE
from app.voice.engines import ScriptedASR, ToneTTS, VoiceEngines, set_engines


def tone(seconds: float) -> np.ndarray:
    n = int(seconds * SAMPLE_RATE)
    return (np.sin(np.arange(n) * 2 * np.pi * 200 / SAMPLE_RATE) * 8000).astype(np.int16)


def say(ws, speech=0.8, pause=0.9):
    audio = np.concatenate([tone(speech), np.zeros(int(pause * SAMPLE_RATE), dtype=np.int16)])
    for start in range(0, len(audio), 1600):
        ws.send_bytes(audio[start:start + 1600].tobytes())


def until(ws, kind, events=None):
    events = [] if events is None else events
    while True:
        message = ws.receive()
        assert message["type"] != "websocket.close", f"closed {message.get('code')}"
        if message.get("bytes") is not None:
            continue
        event = json.loads(message["text"])
        events.append(event)
        if event["type"] == "say_end":
            ws.send_json({"type": "played", "turn": event["turn"], "id": event["id"], "done": True,
                          "ms": event["samples"] / 16})
        if event["type"] == kind:
            return events


def set_tenant(slug, **fields):
    with new_session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one()
        for k, v in fields.items():
            setattr(tenant, k, v)
        db.commit()


@pytest.fixture()
def voice(client, login, settings):
    asr = ScriptedASR()
    set_engines(VoiceEngines(asr, ToneTTS(ms_per_char=20)))
    headers = login("customer@echo.demo")
    token = headers["Authorization"].split(" ", 1)[1]
    conversation = client.post("/api/conversations", json={}, headers=headers).json()["id"]

    class Voice:
        def __init__(self):
            self.asr, self.conversation, self.headers, self.settings = asr, conversation, headers, settings

        def connect(self):
            return _Connection(client, token, conversation)

        def messages(self):
            return client.get(f"/api/conversations/{conversation}/messages", headers=headers).json()["messages"]

    settings.voice_disclosure = False  # on for the tests below that are about it
    yield Voice()
    set_engines(None, None)


class _Connection:
    def __init__(self, client, token, conversation):
        self.client, self.token, self.conversation = client, token, conversation

    def __enter__(self):
        self.cm = self.client.websocket_connect("/api/voice/ws")
        ws = self.cm.__enter__()
        ws.send_json({"type": "auth", "token": self.token, "conversation_id": self.conversation, "mode": "continuous"})
        assert json.loads(ws.receive()["text"])["type"] == "ready"
        return ws

    def __exit__(self, *exc):
        self.cm.__exit__(*exc)
        for worker in threading.enumerate():
            if worker.name == "loopdesk-voice-in":
                worker.join(timeout=30)


# ---------------------------------------------------------------- the disclosure
def test_the_call_opens_with_the_tenant_s_disclosure(voice):
    voice.settings.voice_disclosure = True
    set_tenant("echo", governance={"disclosure": {"voice": "您好，这里是{tenant}的AI语音助手，说“转人工”可以找真人。"}})
    with voice.connect() as ws:
        events = until(ws, "turn_saved")
    prompt = next(e for e in events if e["type"] == "prompt")
    assert prompt["kind"] == "disclosure" and prompt["text"].startswith("您好，这里是Echo音乐的AI语音助手")
    assert any(e["type"] == "say" for e in events)  # spoken, not only shown
    stored = [m for m in voice.messages() if m["role"] == "system"]
    assert stored and stored[0]["meta"]["voice"]["kind"] == "disclosure"


def test_talking_over_the_disclosure_keeps_it_on_record(voice):
    # The customer starts at once: the line is cut, but the conversation still shows what the channel said,
    # with what was heard alongside (a transcript has to prove the disclosure was made).
    voice.settings.voice_disclosure = True
    voice.asr.transcripts += ["怎么取消自动续费"]
    with voice.connect() as ws:
        until(ws, "say")
        say(ws)
        events = until(ws, "turn_saved")
    assert events[-1]["interrupted"] is True
    line = next(m for m in voice.messages() if m["role"] == "system")
    assert line["content"].startswith("您好，我是Echo音乐的智能语音客服")
    assert line["meta"]["voice"]["interrupted"] is True and line["meta"]["voice"]["spoken"] != line["content"]


def test_without_wording_the_default_disclosure_is_spoken_once(voice):
    voice.settings.voice_disclosure = True
    voice.asr.transcripts += ["怎么取消自动续费"]
    with voice.connect() as ws:
        events = until(ws, "turn_saved")
        ws.send_json({"type": "hello"})  # a client cannot make it repeat
        say(ws)
        until(ws, "turn_saved", events)
    prompts = [e for e in events if e["type"] == "prompt" and e["kind"] == "disclosure"]
    assert len(prompts) == 1 and "人工智能" in prompts[0]["text"] and "转人工" in prompts[0]["text"]


def test_disclosure_text_falls_back_per_channel():
    class T:
        name, governance = "Harbourline Bank", {"disclosure": {"web": "網頁版"}}

    assert disclosure.text(T(), "web") == "網頁版"
    assert "Harbourline Bank" in disclosure.text(T(), "voice")  # no voice wording: the voice default


# ---------------------------------------------------------------- reply script
def test_script_conversion_is_at_the_edge_only():
    class Hant:
        locale = "zh-Hant-HK"

    class Hans:
        locale = "zh-Hans"

    assert script.display(Hant(), "退款会在1-3个工作日原路返回") == "退款會在1-3個工作日原路返回"
    assert script.display(Hans(), "退款会在") == "退款会在"
    assert script.canonical(Hant(), "我想問退款") == "我想问退款"
    assert script.event(Hant(), {"type": "token", "text": "订单"})["text"] == "訂單"
    assert script.event(Hant(), {"type": "tool_call", "name": "get_order"}) == {"type": "tool_call", "name": "get_order"}


def test_a_traditional_tenant_hears_and_reads_traditional(voice):
    set_tenant("echo", locale="zh-Hant-HK")
    voice.asr.transcripts += ["會員退款幾耐到賬"]  # Cantonese, in Traditional, as SenseVoice writes it
    with voice.connect() as ws:
        say(ws)
        events = until(ws, "turn_saved")
    says = [e for e in events if e["type"] == "say"]
    assert says and all(script.display(type("T", (), {"locale": "zh-Hant-HK"})(), e["written"]) == e["written"]
                        for e in says)  # already Traditional
    transcript = next(e for e in events if e["type"] == "transcript")
    assert transcript["text"] == "會員退款幾耐到賬"  # shown as the customer said it
    with new_session() as db:
        stored = db.execute(select(Message).where(Message.conversation_id == voice.conversation)
                            .order_by(Message.id)).scalars().all()
    user, answer = stored[0], next(m for m in stored if m.role == "assistant")
    assert user.content == "会员退款几耐到账"  # the agent and the knowledge base work in Simplified
    assert script.display(type("T", (), {"locale": "zh-Hans"})(), answer.content) == answer.content
    listed = next(m for m in voice.messages() if m["role"] == "assistant")
    assert listed["content"] == script.display(type("T", (), {"locale": "zh-Hant-HK"})(), answer.content)
    assert listed["content"] != answer.content


def test_text_chat_streams_traditional_too(client, login):
    set_tenant("echo", locale="zh-Hant-HK")
    headers = login("customer@echo.demo")
    conversation = client.post("/api/conversations", json={}, headers=headers).json()["id"]
    with client.stream("POST", f"/api/conversations/{conversation}/stream", json={"content": "会员退款多久能到账"},
                       headers=headers) as response:
        events = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
    final = next(e for e in events if e["type"] == "final")
    assert final["text"] and script.display(type("T", (), {"locale": "zh-Hant-HK"})(), final["text"]) == final["text"]


# ---------------------------------------------------------------- the keypad
def agent_calls(events):
    return [e["event"] for e in events if e["type"] == "agent" and e["event"]["type"] == "tool_call"]


def test_keyed_order_number_goes_to_the_agent_without_a_read_back(voice):
    with voice.connect() as ws:
        ws.send_json({"type": "keys", "kind": "order_id", "digits": "20261019101"})
        events = until(ws, "turn_saved")
    transcript = next(e for e in events if e["type"] == "transcript")
    assert transcript["text"] == "订单号SO20261019101" and transcript["endpoint"] == "keys"
    assert not any(e["type"] == "prompt" for e in events)  # typed digits are not read back
    assert any(c["name"] == "get_order" and c["arguments"]["order_id"] == "SO20261019101" for c in agent_calls(events))


def test_keys_correct_the_number_a_read_back_was_unsure_of(voice):
    # The two passes disagree on the last digit: the channel asks; the customer keys the number instead.
    voice.asr.transcripts += [{"final": "帮我查一下订单 SO二零二六一零一九一零七", "streaming": "帮我查一下订单 SO二零二六一零一九一零一"}]
    with voice.connect() as ws:
        say(ws)
        events = until(ws, "turn_saved")
        assert any(e["type"] == "prompt" and e["kind"] == "readback" for e in events)
        ws.send_json({"type": "keys", "kind": "order_id", "digits": "SO20261019101"})
        events = until(ws, "turn_saved")
    assert next(e for e in events if e["type"] == "transcript")["text"] == "订单号SO20261019101"  # what was typed
    asked = [m["content"] for m in voice.messages() if m["role"] == "user"]
    assert asked == ["帮我查一下订单SO20261019101"]  # the request, with the typed number in it
    assert any(c["name"] == "get_order" and c["arguments"]["order_id"] == "SO20261019101" for c in agent_calls(events))


def test_a_keyed_amount_and_bad_keys(voice):
    with voice.connect() as ws:
        ws.send_json({"type": "keys", "kind": "amount", "digits": "18.5"})
        events = until(ws, "transcript")
        assert events[-1]["text"] == "金额18.5元"
        until(ws, "turn_saved")
        for bad in ({"kind": "amount", "digits": "1e9"}, {"kind": "order_id", "digits": "123"},
                    {"kind": "phone", "digits": "13812345678"}):
            ws.send_json({"type": "keys", **bad})
            assert until(ws, "error")[-1]["message"]
