"""Voice channel end to end over the WebSocket, with fake speech engines (no models needed):
endpointing, transcript handling, the spoken confirmation in front of money tools, PII masking,
and barge-in keeping only what the customer heard."""

import json
import threading
import time
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
            # The test client cancels the handler once the socket closes; the session then closes in its
            # own thread (saving what was heard). A server lets the handler finish, so wait for it here.
            for worker in threading.enumerate():
                if worker.name == "loopdesk-voice-in":
                    worker.join(timeout=30)

    yield Voice()
    set_engines(None, None)


def say(ws, speech=0.8, pause=0.9, pace=0.0):
    """Send speech and a pause in 100 ms frames; ``pace`` seconds between frames (a browser sends ten a
    second; tests that are about the backlog send them faster than that, but not in one burst)."""
    import time as _time

    audio = np.concatenate([tone(speech), silence(pause)])
    for start in range(0, len(audio), 1600):
        ws.send_bytes(audio[start:start + 1600].tobytes())
        if pace:
            _time.sleep(pace)


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


# ---------------------------------------------------------------- review round 1 (second model)
def test_confirmation_only_counts_once_the_read_back_was_heard(voice):
    # P0: the customer cut the read-back off before the amount was said; "确认" must not move money.
    voice.asr.transcripts += ["SO二零二六一零一九一零一 帮我退款", "嗯", "确认"]
    with voice.connect() as ws:
        say(ws)
        readback = until(ws, "say", play=False)[-1]
        until(ws, "turn_end", play=False)
        say(ws, speech=0.8, pause=0.0)  # talks over the read-back
        until(ws, "barge_in", play=False)
        ws.send_json({"type": "played", "turn": readback["turn"], "id": readback["id"], "ms": 200})
        ws.send_bytes(silence(0.9).tobytes())
        events = until(ws, "turn_saved", stop_at="error")  # the interrupted turn is saved
        events += until(ws, "turn_saved")  # the answer to "嗯": the read-back again
        spoken = "".join(e["text"] for e in events if e["type"] == "say")
        assert "18元" in spoken and refunds() == []
        say(ws)
        until(ws, "turn_saved")
    filed = refunds()
    assert len(filed) == 1 and filed[0].amount == 18.0  # heard in full this time, then confirmed


def test_entity_read_back_does_not_authorise_a_refund(voice):
    # P1: "订单号是X，金额是Y元，对吗？" never mentioned a refund; the money gate still asks.
    voice.asr.transcripts += [{"final": "SO二零二六一零一九一零一 退款十八块", "streaming": "SO二零二六一零一九一零七 退款十八块"},
                              "对的"]
    with voice.connect() as ws:
        say(ws)
        until(ws, "turn_saved")
        say(ws)
        events = until(ws, "turn_saved")
    spoken = "".join(e["text"] for e in events if e["type"] == "say")
    assert "资金安全" in spoken and refunds() == []


def test_unused_confirmations_expire_with_the_turn(voice):
    from app.voice.confirm import Confirmations

    c = Confirmations()
    c.request("request_refund", {"order_id": "SO20261019101", "amount": 18.0}, "x")
    c.accept()
    c.new_turn()  # the next utterance: whatever was confirmed and not used is gone
    assert not c.take("request_refund", {"order_id": "SO20261019101", "amount": 18.0})


def test_a_disconnect_is_not_an_interruption(voice):
    # P1: the page closed (or the network dropped) mid-answer; nobody talked over it.
    voice.asr.transcripts += ["怎么取消自动续费"]
    with voice.connect() as ws:
        say(ws)
        until(ws, "turn_end", play=False)
    with new_session() as db:
        answer = db.execute(select(Message).where(Message.conversation_id == voice.conversation,
                                                  Message.role == "assistant")).scalar_one()
    assert answer.meta["voice"]["interrupted"] is False and answer.meta["voice"]["delivery"] == "unknown"
    assert "管理自动续费" in answer.content and "打断" not in answer.content


def test_spoken_confirmation_is_audited(voice):
    from app.models import AuditLog

    voice.asr.transcripts += ["SO二零二六一零一九一零一 帮我退款", "确认"]
    with voice.connect() as ws:
        say(ws)
        until(ws, "turn_saved")
        say(ws)
        until(ws, "turn_saved")
    with new_session() as db:
        entry = db.execute(select(AuditLog).where(AuditLog.action == "voice.confirm")).scalar_one()
        user = db.execute(select(Message).where(Message.conversation_id == voice.conversation, Message.role == "user")
                          .order_by(Message.id.desc())).scalars().first()
    assert entry.detail["order_id"] == "SO20261019101" and entry.detail["amount"] == 18.0
    assert entry.detail["readback_heard"] is True and entry.detail["reply"] == "确认"
    assert user.meta["voice"]["reply"] == "确认"


def test_a_client_that_sends_faster_than_asr_decodes_is_cut_off(voice, monkeypatch):
    import time as _time

    from app.api import voice as voice_api

    class SlowASR(ScriptedASR):
        def stream(self, hotwords):
            stream = super().stream(hotwords)
            accept = stream.accept
            stream.accept = lambda pcm: (_time.sleep(0.05), accept(pcm))[1]
            return stream

    set_engines(VoiceEngines(SlowASR(["x"]), ToneTTS()))
    monkeypatch.setattr(voice_api, "MAX_BACKLOG", 5)
    with voice.connect() as ws:
        for _ in range(60):
            ws.send_bytes(tone(0.1).tobytes())
        closed = None
        while closed is None:
            message = ws.receive()
            if message["type"] == "websocket.close":
                closed = message["code"]
    assert closed == 1013


def test_engines_are_retried_after_a_failure(monkeypatch):
    from app.config import Settings
    from app.voice import engines

    calls = []
    monkeypatch.setattr(engines, "build_engines", lambda s, asr=None: (calls.append(1), (None, "sidecar down", None))[1])
    clock = [1000.0]
    monkeypatch.setattr(engines, "_clock", lambda: clock[0])
    engines.set_engines(None, None)
    s = Settings()
    assert engines.get_engines(s) == (None, "sidecar down")
    engines.get_engines(s)
    assert len(calls) == 1  # not on every connection
    clock[0] += engines.RETRY_SECONDS + 1
    engines.get_engines(s)
    assert len(calls) == 2
    engines.set_engines(None, None)


def test_a_confirmation_left_over_from_an_earlier_turn_does_not_authorise_a_refund(voice):
    import time as _time

    from app.config import get_settings
    from app.models import Conversation
    from app.services import get_services
    from app.voice.session import Transcript, VoiceSession

    with new_session() as db:
        conversation = db.get(Conversation, voice.conversation)
        tenant_id, user_id = conversation.tenant_id, conversation.user_id
    events = []
    session = VoiceSession(engines=VoiceEngines(ScriptedASR(), ToneTTS()), services=get_services(), settings=get_settings(),
                           tenant_id=tenant_id, user_id=user_id, conversation_id=voice.conversation, emit=events.append,
                           db_factory=new_session, assume_played=True)
    # A refund confirmed earlier that the run never used (the model asked a question back, the conversation
    # went to a person, ...): it must not be spent on a later request.
    session.confirmations.allow("request_refund", "SO20261019101", 18.0)
    session._decide(Transcript("SO20261019101 帮我退款"), {}, _time.perf_counter())
    session.close()
    assert refunds() == []
    assert any(e.get("type") == "say" and "资金安全" in e.get("text", "") for e in events if isinstance(e, dict))


# ---------------------------------------------------------------- review round 2
def until_open(ws, kind):
    """Like until(), but a closed socket fails the test instead of hanging it."""
    events = []
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


def test_a_slow_answer_does_not_cut_the_customer_off(voice, monkeypatch):
    # P1: the worker waited for the previous run while the browser kept streaming: the backlog filled
    # and the session was closed (1013) on a run that was merely slow.
    from app.api import voice as voice_api
    from app.services import get_services

    monkeypatch.setattr(voice_api, "MAX_BACKLOG", 12)
    monkeypatch.setattr(get_services().provider, "stream_delay", 0.15)
    voice.asr.transcripts += ["怎么取消自动续费", "会员退款多久能到账"]
    with voice.connect() as ws:
        say(ws, pace=0.01)
        say(ws, pace=0.01)  # a second sentence while the first answer is still being written
        for _ in range(40):  # the browser keeps streaming: 40 frames in 0.4 s, ten times real time
            ws.send_bytes(silence(0.1).tobytes())
            time.sleep(0.01)
        until_open(ws, "turn_saved")
        until_open(ws, "turn_saved")


def test_saying_yes_right_after_the_amount_confirms(voice):
    # P1: the amount is its own sentence; talking over "确认请说确认……" after hearing it is a yes.
    voice.asr.transcripts += ["SO二零二六一零一九一零一 帮我退款", "确认"]
    with voice.connect() as ws:
        say(ws)
        events = until(ws, "turn_end", play=False)
        says = [e for e in events if e["type"] == "say"]
        ends = {e["id"]: e["samples"] for e in events if e["type"] == "say_end"}
        amount, tail = says[-2], says[-1]
        assert "18元" in amount["text"] and "确认" in tail["text"]
        ws.send_json({"type": "played", "turn": amount["turn"], "id": amount["id"], "done": True,
                      "ms": ends[amount["id"]] / 16})
        ws.send_json({"type": "played", "turn": tail["turn"], "id": tail["id"], "ms": 300})
        say(ws)  # "确认", over the instruction
        until(ws, "turn_saved")  # the read-back turn, cut short
        until(ws, "turn_saved")  # the confirmed refund
    assert len(refunds()) == 1


def test_an_unclear_reply_asks_again_and_keeps_the_refund_pending(voice):
    voice.asr.transcripts += ["SO二零二六一零一九一零一 帮我退款", "呃", "确认"]
    with voice.connect() as ws:
        say(ws)
        until(ws, "turn_saved")
        say(ws)
        events = until(ws, "turn_saved")
        assert any(e["type"] == "prompt" and e["kind"] == "readback_unclear" for e in events)
        say(ws)
        until(ws, "turn_saved")
    assert len(refunds()) == 1


def test_read_backs_that_cannot_be_played_give_up(voice):
    class MuteTTS(ToneTTS):
        def synthesize(self, text):
            return iter(())

    set_engines(VoiceEngines(voice.asr, MuteTTS()))
    voice.asr.transcripts += ["SO二零二六一零一九一零一 帮我退款", "确认", "确认", "确认"]
    with voice.connect() as ws:
        for _ in range(4):
            say(ws)
            events = until(ws, "turn_saved")
    assert any(e["type"] == "prompt" and e["kind"] == "readback_failed" for e in events)
    assert refunds() == []


def test_a_failing_session_close_does_not_hang_the_socket(voice, monkeypatch):
    import threading as _threading

    from app.voice.session import VoiceSession

    def broken(self):
        raise RuntimeError("database gone")

    monkeypatch.setattr(VoiceSession, "close", broken)
    finished = _threading.Event()

    def run():
        with voice.connect():
            pass
        finished.set()

    worker = _threading.Thread(target=run, daemon=True)
    worker.start()
    assert finished.wait(10), "the handler never returned"


def test_retrying_engines_keeps_the_loaded_asr(monkeypatch):
    from app.config import Settings
    from app.voice import engines

    built = []

    class FakeParaformer(ScriptedASR):
        name = "paraformer"

        def __init__(self, device="cpu"):
            super().__init__()
            built.append(1)

    def down(url, speaker=None):
        raise ConnectionError("sidecar down")

    monkeypatch.setattr(engines, "ParaformerASR", FakeParaformer)
    monkeypatch.setattr(engines, "CosyVoiceTTS", down)
    clock = [1000.0]
    monkeypatch.setattr(engines, "_clock", lambda: clock[0])
    engines.set_engines(None, None)
    s = Settings()
    s.voice_asr, s.voice_tts = "paraformer", "cosyvoice"
    assert engines.get_engines(s)[0] is None
    clock[0] += engines.RETRY_SECONDS + 1
    assert engines.get_engines(s)[0] is None
    assert len(built) == 1  # the ASR models were loaded once, only the sidecar is tried again
    engines.set_engines(None, None)


def test_a_newer_utterance_interrupts_the_answer_before_it(voice, monkeypatch):
    # Round 3: three questions in a row while the first answer is still being written. The second
    # answer is superseded by the third question: that is an interruption (the client is told to stop
    # it), not an answer of unknown delivery the model would take as said.
    from app.services import get_services

    monkeypatch.setattr(get_services().provider, "stream_delay", 0.1)
    voice.asr.transcripts += ["怎么取消自动续费", "会员退款多久能到账", "学生开会员有优惠吗"]
    with voice.connect() as ws:
        say(ws)
        say(ws)
        say(ws)
        events = []
        while sum(e["type"] == "turn_saved" for e in events) < 3:
            # The client never reports the second answer as played: it is still playing when the third
            # question is taken up (acknowledging it could let it finish first, then nothing supersedes it).
            read = until(ws, "say_end", play=False, stop_at="turn_saved")
            events += read
            event = read[-1]
            if event["type"] == "say_end" and event["turn"] != 2:
                ws.send_json({"type": "played", "turn": event["turn"], "id": event["id"], "done": True,
                              "ms": event["samples"] / 16})
    with new_session() as db:
        answers = db.execute(select(Message).where(Message.conversation_id == voice.conversation,
                                                   Message.role == "assistant").order_by(Message.id)).scalars().all()
    assert len(answers) == 3
    assert answers[1].meta["voice"]["interrupted"] is True and answers[1].meta["voice"]["delivery"] == "interrupted"
    second_turn = next(e["turn"] for e in events if e["type"] == "turn_saved" and e["message_id"] == answers[1].id)
    assert any(e["type"] == "barge_in" and e["turn"] == second_turn for e in events)


# ---------------------------------------------------------------- punctuation (LOOPDESK_VOICE_PUNC)
class _Unpunctuated:
    """A streaming engine that writes no punctuation, like Paraformer: partials while audio comes in,
    then the final pass."""

    name, punctuates = "paraformer", False

    def __init__(self, final: str, streaming: str | None = None):
        self.final, self.streaming = final, streaming if streaming is not None else final

    def stream(self, hotwords):
        from app.voice.engines import ASRResult

        engine = self

        class Stream:
            def accept(self, pcm):
                return engine.streaming[: len(engine.streaming) // 2] or None

            def finish(self):
                return ASRResult(engine.final, {"streaming": engine.streaming, "final": engine.final}, final_ms=100.0)

        return Stream()


def _split_requests(text: str) -> str:
    return text.replace("退款另外", "退款，另外") + "？"


def test_punctuation_goes_on_the_final_text_only():
    from app.voice.engines import PunctuatedASR
    from app.voice.session import understand

    seen = []
    final = "so二零二六零九一八零零一我要退货退款另外发票怎么开"
    asr = PunctuatedASR(_Unpunctuated(final), lambda text: seen.append(text) or _split_requests(text))
    assert asr.name == "paraformer" and asr.punctuates
    stream = asr.stream([])
    assert stream.accept(silence(0.1)) == final[: len(final) // 2]  # partials are shown as decoded
    result = stream.finish()
    assert seen == [final]
    assert result.text == "so二零二六零九一八零零一我要退货退款，另外发票怎么开？"
    assert result.passes["final"] == final  # the decoder's own output stays a pass of its own
    assert result.punc_ms is not None and result.final_ms == pytest.approx(100.0 + result.punc_ms)
    transcript = understand(result, Lexicon([]))
    assert transcript.text == "SO20260918001我要退货退款，另外发票怎么开？"
    assert [(e.kind, e.value, e.confidence) for e in transcript.entities] == [("order_id", "SO20260918001", 0.95)]


def test_an_empty_utterance_is_not_sent_to_the_punctuation_model():
    from app.voice.engines import PunctuatedASR

    seen = []
    result = PunctuatedASR(_Unpunctuated(""), lambda text: seen.append(text) or text).stream([]).finish()
    assert result.text == "" and seen == [] and result.punc_ms is None


def test_a_failing_punctuation_model_keeps_the_words(caplog):
    from app.voice.engines import PunctuatedASR

    def broken(text):
        raise RuntimeError("index out of range")

    with caplog.at_level("WARNING", logger="app.voice.engines"):
        result = PunctuatedASR(_Unpunctuated("帮我查一下物流"), broken).stream([]).finish()
    assert result.text == "帮我查一下物流" and result.punc_ms is None  # the turn goes on, unpunctuated
    assert "punctuation failed" in caplog.text


def test_an_amount_the_punctuation_split_is_read_back():
    # The unpunctuated final pass is a second reading: punctuation that changes an entity is caught.
    from app.voice.engines import PunctuatedASR
    from app.voice.session import understand

    asr = PunctuatedASR(_Unpunctuated("先退我一百九十八块"), lambda text: text.replace("一百", "一百，"))
    transcript = understand(asr.stream([]).finish(), Lexicon([]))
    amount = next(e for e in transcript.entities if e.kind == "amount")
    assert amount.value == "98" and amount.confidence < 0.6 and "passes_disagree" in amount.notes


def test_punctuation_setting_wraps_only_engines_that_write_none(monkeypatch, caplog):
    from app.config import Settings
    from app.voice import engines

    class FakeParaformer(ScriptedASR):
        name, punctuates = "paraformer", False

        def __init__(self, device="cpu"):
            super().__init__()

    class FakeSenseVoice(ScriptedASR):
        name, punctuates = "sensevoice", True

        def __init__(self, device="cpu"):
            super().__init__()

    loaded = []

    class FakePunctuator:
        def __init__(self, device="cpu"):
            loaded.append(device)

        def __call__(self, text):
            return text + "。"

    monkeypatch.setattr(engines, "ParaformerASR", FakeParaformer)
    monkeypatch.setattr(engines, "SenseVoiceASR", FakeSenseVoice)
    monkeypatch.setattr(engines, "Punctuator", FakePunctuator)
    s = Settings()
    s.voice_asr, s.voice_tts, s.seed_demo = "paraformer", "tone", True
    assert s.voice_punc is False  # off by default
    built, problem, _ = engines.build_engines(s)
    assert problem is None and isinstance(built.asr, FakeParaformer) and loaded == []
    assert built.describe()["punctuation"] is False

    s.voice_punc = True
    built, problem, _ = engines.build_engines(s)
    assert isinstance(built.asr, engines.PunctuatedASR) and built.asr.name == "paraformer" and loaded == ["cpu"]
    assert built.describe()["punctuation"] is True

    s.voice_asr = "sensevoice"  # writes its own punctuation: nothing to add, no model loaded
    built, _, _ = engines.build_engines(s)
    assert isinstance(built.asr, FakeSenseVoice) and loaded == ["cpu"]

    def missing(device="cpu"):
        raise ImportError("No module named 'funasr'")

    monkeypatch.setattr(engines, "Punctuator", missing)
    s.voice_asr = "paraformer"
    with caplog.at_level("WARNING", logger="app.voice.engines"):
        built, problem, _ = engines.build_engines(s)
    assert problem is None and isinstance(built.asr, FakeParaformer)  # the channel works, unpunctuated
    assert "punctuation" in caplog.text


def test_voice_punc_is_read_from_the_environment(monkeypatch):
    from app.config import Settings

    assert Settings.from_env().voice_punc is False
    monkeypatch.setenv("LOOPDESK_VOICE_PUNC", "1")
    assert Settings.from_env().voice_punc is True


def test_the_agent_receives_the_punctuated_transcript(voice):
    from app.voice.engines import PunctuatedASR

    voice.asr.transcripts.append("怎么取消自动续费另外学生开会员有优惠吗")
    set_engines(VoiceEngines(PunctuatedASR(voice.asr, lambda text: text.replace("续费另外", "续费，另外")),
                             ToneTTS(ms_per_char=20)))
    with voice.connect() as ws:
        say(ws)
        events = until(ws, "turn_saved")
    transcript = next(e for e in events if e["type"] == "transcript")
    assert transcript["text"] == "怎么取消自动续费，另外学生开会员有优惠吗"
    with new_session() as db:
        asked = db.execute(select(Message).where(Message.conversation_id == voice.conversation,
                                                 Message.role == "user")).scalars().all()
    assert [m.content for m in asked] == ["怎么取消自动续费，另外学生开会员有优惠吗"]
