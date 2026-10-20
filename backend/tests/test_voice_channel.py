"""Voice channel end to end over the WebSocket, with fake speech engines (no models needed):
endpointing, transcript handling, the spoken confirmation in front of money tools, PII masking,
and barge-in keeping only what the customer heard."""

import json
from contextlib import contextmanager

import numpy as np
import pytest
from sqlalchemy import select

from app.agent.business_tools import build_registry
from app.agent.tools import ToolError
from app.db import new_session
from app.models import Message, Refund, Run, Tenant
from app.voice.confirm import VOICE_CONFIRM_ERROR, Confirmations, gate_registry
from app.voice.endpoint import FRAME, SAMPLE_RATE, EnergyVAD, Endpointer, endpoint_wait_ms
from app.voice.engines import ScriptedASR, ToneTTS, VoiceEngines, set_engines
from app.voice.lexicon import Lexicon, lexicon_for


def tone(seconds: float, amplitude: int = 8000) -> np.ndarray:
    n = int(seconds * SAMPLE_RATE)
    return (np.sin(np.arange(n) * 2 * np.pi * 200 / SAMPLE_RATE) * amplitude).astype(np.int16)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.int16)


# ---------------------------------------------------------------- endpointing
def test_endpoint_after_trailing_silence_in_audio_time():
    ep = Endpointer(EnergyVAD(), end_silence_ms=600)
    events = ep.push(np.concatenate([silence(0.5), tone(1.0), silence(1.0)]))
    kinds = [e.kind for e in events]
    assert kinds[0] == "start" and kinds.count("end") == 1
    end = next(e for e in events if e.kind == "end")
    assert end.reason == "silence"
    assert 600 <= endpoint_wait_ms(end) < 600 + 2 * FRAME / SAMPLE_RATE * 1000
    start = events[0]
    assert start.at <= int(0.5 * SAMPLE_RATE)  # the preroll keeps the onset


def test_short_pause_does_not_end_the_utterance():
    ep = Endpointer(EnergyVAD(), end_silence_ms=600)
    events = ep.push(np.concatenate([tone(0.6), silence(0.3), tone(0.6), silence(1.0)]))
    assert [e.kind for e in events].count("end") == 1


def test_push_to_talk_ends_on_release_without_waiting():
    ep = Endpointer(EnergyVAD(), end_silence_ms=600)
    ep.auto = False
    assert ep.push(tone(0.5)) == []  # push-to-talk mode: speech alone starts nothing
    ep.press()
    ep.push(tone(0.5))
    end = [e for e in ep.release() if e.kind == "end"][0]
    assert end.reason == "released" and endpoint_wait_ms(end) == 0


def test_utterance_length_is_capped():
    ep = Endpointer(EnergyVAD(), max_utterance_ms=2000)
    ends = [e for e in ep.push(tone(3.0)) if e.kind == "end"]
    assert ends and ends[0].reason == "max_length"


# ---------------------------------------------------------------- hotwords
def test_pinyin_correction_fixes_a_misheard_plan_name():
    lex = Lexicon(["豪华会员", "连续包月", "年卡"])
    text, fixes = lex.correct("好华会员连续包月多少钱")
    assert text == "豪华会员连续包月多少钱" and fixes == [("好华会员", "豪华会员")]
    assert lex.correct("年卡多少钱")[1] == []  # two-character terms are not fuzzy-matched


def test_aliases_cover_latin_brand_words():
    lex = Lexicon(["Echo币"], {"Echo币": ["一口币"]})
    assert lex.correct("一块钱能充多少一口币")[0] == "一块钱能充多少Echo币"


def test_tenant_hotwords_come_from_its_knowledge_and_products(client):
    with new_session() as db:
        echo = db.execute(select(Tenant).where(Tenant.slug == "echo")).scalar_one()
        terms = lexicon_for(db, echo).terms
    assert {"豪华会员", "Echo币", "年卡", "连续包月", "数字专辑"} <= set(terms)
    assert "扫地机器人" not in terms  # another tenant's product


# ---------------------------------------------------------------- the confirmation gate
class _Ctx:
    approval_id = None


def test_money_tools_need_a_confirmation_each_time(monkeypatch):
    base = build_registry()
    confirmations = Confirmations()
    gated = gate_registry(base, confirmations, lambda: "SO20261019101 帮我退款")
    refund = gated.get("request_refund")
    monkeypatch.setattr(base.get("request_refund"), "precheck", lambda ctx, args: {"amount": 18.0})
    gated = gate_registry(base, confirmations, lambda: "SO20261019101 帮我退款")
    refund = gated.get("request_refund")
    assert gated.get("get_order") is base.get("get_order")  # read tools untouched
    with pytest.raises(ToolError) as exc:
        refund.precheck(_Ctx(), {"order_id": "so20261019101", "reason": "x"})
    assert str(exc.value) == VOICE_CONFIRM_ERROR
    assert confirmations.pending.order_id == "SO20261019101" and confirmations.pending.amount == 18.0
    confirmations.accept()
    assert refund.precheck(_Ctx(), {"order_id": "SO20261019101", "reason": "x"}) == {"amount": 18.0}
    with pytest.raises(ToolError):  # one confirmation, one call
        refund.precheck(_Ctx(), {"order_id": "SO20261019101", "reason": "x"})
    approved = _Ctx()
    approved.approval_id = 7  # a reviewer's decision being carried out: the customer confirmed earlier
    assert refund.precheck(approved, {"order_id": "SO20261019101", "reason": "x"}) == {"amount": 18.0}


# ---------------------------------------------------------------- over the WebSocket
@pytest.fixture()
def voice(client, login):
    asr = ScriptedASR()
    set_engines(VoiceEngines(asr, ToneTTS(ms_per_char=20)))
    headers = login("customer@echo.demo")
    token = headers["Authorization"].split(" ", 1)[1]
    conversation = client.post("/api/conversations", json={}, headers=headers).json()["id"]

    class Voice:
        def __init__(self):
            self.asr, self.token, self.conversation, self.headers = asr, token, conversation, headers

        @contextmanager
        def connect(self, mode="continuous"):
            with client.websocket_connect("/api/voice/ws") as ws:
                ws.send_json({"type": "auth", "token": token, "conversation_id": conversation, "mode": mode})
                ready = json.loads(ws.receive()["text"])
                assert ready["type"] == "ready", ready
                yield ws

    yield Voice()
    set_engines(None, None)


def say(ws, speech=0.8, pause=0.9):
    audio = np.concatenate([tone(speech), silence(pause)])
    for start in range(0, len(audio), 1600):
        ws.send_bytes(audio[start:start + 1600].tobytes())


def until(ws, kind, *, play=True, stop_at=None):
    """Read events up to ``kind``; acknowledge every finished sentence as played (like a client)."""
    events, audio = [], 0
    while True:
        message = ws.receive()
        if message.get("bytes") is not None:
            audio += len(message["bytes"]) - 4
            continue
        event = json.loads(message["text"])
        events.append(event)
        if play and event["type"] == "say_end":
            ws.send_json({"type": "played", "turn": event["turn"], "id": event["id"], "done": True,
                          "ms": event["samples"] / 16})
        if event["type"] == kind or (stop_at and event["type"] == stop_at):
            return events


def refunds() -> list[Refund]:
    with new_session() as db:
        return db.execute(select(Refund).where(Refund.order_id == "SO20261019101")).scalars().all()


def test_voice_is_off_unless_engines_are_configured(client, login):
    set_engines(None, None)
    headers = login("customer@echo.demo")
    config = client.get("/api/voice/config", headers=headers).json()
    assert config["enabled"] is False and config["reason"]
    conversation = client.post("/api/conversations", json={}, headers=headers).json()["id"]
    with client.websocket_connect("/api/voice/ws") as ws:
        ws.send_json({"type": "auth", "token": headers["Authorization"][7:], "conversation_id": conversation})
        message = ws.receive()
        assert message["type"] == "websocket.close" and message["code"] == 4503
    set_engines(None, None)


def test_websocket_requires_a_valid_token_and_own_conversation(client, login, voice):
    with client.websocket_connect("/api/voice/ws") as ws:
        ws.send_json({"type": "auth", "token": "nope", "conversation_id": voice.conversation})
        assert ws.receive()["code"] == 4401
    other = login("customer2@echo.demo")
    with client.websocket_connect("/api/voice/ws") as ws:
        ws.send_json({"type": "auth", "token": other["Authorization"][7:], "conversation_id": voice.conversation})
        assert ws.receive()["code"] == 4404


def test_refund_by_voice_is_read_back_and_runs_only_after_yes(voice):
    voice.asr.transcripts += ["S O 二零二六一零一九一零一 刚被扣了续费 帮我退款", "确认"]
    with voice.connect() as ws:
        say(ws)
        events = until(ws, "turn_saved")
        transcript = next(e for e in events if e["type"] == "transcript")
        assert transcript["text"] == "SO20261019101刚被扣了续费帮我退款"
        spoken = "".join(e["text"] for e in events if e["type"] == "say")
        assert "二零二六" in spoken and "18元" in spoken and "确认" in spoken  # order and amount read back
        assert refunds() == []  # nothing filed yet
        say(ws)
        events = until(ws, "turn_saved")
        filed = refunds()
        assert len(filed) == 1 and filed[0].amount == 18.0
        results = [e["event"] for e in events if e["type"] == "agent" and e["event"]["type"] == "tool_result"]
        assert any(r["name"] == "request_refund" and not r["is_error"] for r in results)
    with new_session() as db:
        users = db.execute(select(Message.content).where(Message.conversation_id == voice.conversation,
                                                         Message.role == "user").order_by(Message.id)).scalars().all()
    assert users[-1].startswith("确认：SO20261019101")


def test_refund_by_voice_cancelled_files_nothing(voice):
    voice.asr.transcripts += ["SO二零二六一零一九一零一 帮我退款", "不对，取消"]
    with voice.connect() as ws:
        say(ws)
        until(ws, "turn_saved")
        say(ws)
        events = until(ws, "turn_saved")
        assert any(e["type"] == "prompt" and e["kind"] == "cancelled" for e in events)
    assert refunds() == []


def test_spoken_phone_number_is_masked_before_the_model_sees_it(voice):
    voice.asr.transcripts += ["我的手机号是一三八一二三四五六七八 帮我查一下订单 S O 二零二六一零一九一零一"]
    with voice.connect() as ws:
        say(ws)
        events = until(ws, "turn_saved")
    transcript = next(e for e in events if e["type"] == "transcript")
    assert "138****5678" in transcript["text"] and "phone" in transcript["pii"]
    assert "13812345678" not in json.dumps(events, ensure_ascii=False)
    with new_session() as db:
        stored = db.execute(select(Message.content).where(Message.conversation_id == voice.conversation)).scalars().all()
        inputs = db.execute(select(Run.input_text).where(Run.conversation_id == voice.conversation)).scalars().all()
    assert all("13812345678" not in text for text in stored + inputs)
    assert any("138****5678" in text for text in stored)


def test_low_confidence_order_number_is_read_back_before_any_run(voice):
    # The streaming pass heard a different last digit than the final pass.
    voice.asr.transcripts += [{"final": "帮我查一下订单 SO二零二六一零一九一零一", "streaming": "帮我查一下订单 SO二零二六一零一九一零七"},
                              "对的"]
    with voice.connect() as ws:
        say(ws)
        events = until(ws, "turn_saved")
        assert any(e["type"] == "prompt" and e["kind"] == "readback" for e in events)
        with new_session() as db:
            assert db.execute(select(Run).where(Run.conversation_id == voice.conversation)).first() is None
        say(ws)
        events = until(ws, "turn_saved")
    calls = [e["event"] for e in events if e["type"] == "agent" and e["event"]["type"] == "tool_call"]
    assert any(c["name"] == "get_order" and c["arguments"]["order_id"] == "SO20261019101" for c in calls)


def test_barge_in_keeps_only_what_was_heard(voice):
    voice.asr.transcripts += ["怎么取消自动续费", "好的知道了"]
    with voice.connect() as ws:
        say(ws)
        events = until(ws, "say", play=False)  # the first sentence starts playing; nothing is acknowledged
        first = events[-1]
        sent = until(ws, "turn_end", play=False)
        samples = next(e["samples"] for e in sent if e["type"] == "say_end" and e["id"] == first["id"])
        say(ws, speech=0.8, pause=0.0)  # the customer talks over the answer
        until(ws, "barge_in", play=False)
        with new_session() as db:
            generated = db.execute(select(Message).where(Message.conversation_id == voice.conversation,
                                                         Message.role == "assistant")).scalar_one().content
        written = first["written"]
        played_ms = samples / 16 / 2  # half of the first sentence's audio
        ws.send_json({"type": "played", "turn": first["turn"], "id": first["id"], "ms": played_ms})
        ws.send_bytes(silence(0.9).tobytes())  # the interruption ends
        events = until(ws, "turn_saved", stop_at="error")
        saved = [e for e in events if e["type"] == "turn_saved"]
        assert saved[0]["interrupted"] is True and saved[0]["heard"] == written[: len(written) // 2]
    with new_session() as db:
        answers = db.execute(select(Message).where(Message.conversation_id == voice.conversation,
                                                   Message.role == "assistant").order_by(Message.id)).scalars().all()
    first_answer = answers[0]
    assert first_answer.content == written[: len(written) // 2] + "……"
    assert first_answer.meta["voice"]["interrupted"] is True
    assert first_answer.meta["voice"]["generated"] == generated


def test_latency_breakdown_is_reported(voice):
    voice.asr.transcripts += ["会员退款多久能到账"]
    with voice.connect() as ws:
        say(ws)
        events = until(ws, "turn_saved")
    latency = next(e for e in events if e["type"] == "turn_end")["latency"]
    assert set(latency) >= {"endpoint_wait_ms", "asr_final_ms", "llm_first_sentence_ms", "tts_first_packet_ms",
                            "first_audio_after_endpoint_ms"}
    assert 600 <= latency["endpoint_wait_ms"] < 700
