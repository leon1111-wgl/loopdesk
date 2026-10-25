"""Semantic endpointing: asking whether the customer is done instead of only counting silence, and
taking an answer back when they turn out not to be. Fake detectors and engines, no models."""

import json
import threading
import time

import numpy as np
import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import new_session
from app.models import Message, Run
from app.voice.endpoint import FRAME, SAMPLE_RATE, EnergyVAD, Endpointer, endpoint_wait_ms
from app.voice.engines import ScriptedASR, ToneTTS, VoiceEngines, set_engines
from app.voice.turn import (COMPLETE, INCOMPLETE, UNSURE, RuleTurn, SmartTurn, Verdict, build_turn_detector,
                            log_mel, unfinished)


def tone(seconds: float, amplitude: int = 8000) -> np.ndarray:
    n = int(seconds * SAMPLE_RATE)
    return (np.sin(np.arange(n) * 2 * np.pi * 200 / SAMPLE_RATE) * amplitude).astype(np.int16)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.int16)


FRAME_MS = FRAME / SAMPLE_RATE * 1000


# ---------------------------------------------------------------- the text rule
@pytest.mark.parametrize("partial,reason", [
    ("我的订单号是S O二零二六一零", "digits"),  # an order number being read out, mid-way
    ("订单号是 SO 2026 1015", "digits"),
    ("手机号一三八一二", "digits"),
    ("我想退款然后", "connective"),
    ("还有", "connective"),
    ("就是那个", "connective"),
    ("呃", "connective"),
    ("我想问一下嗯", "connective"),
    ("订单号是二零二六一零一九", "digits"),
    ("我想問下然後", "connective"),  # Traditional, Cantonese speakers
    ("同埋", "connective"),
    ("仲有", "connective"),
    ("即係", "connective"),
])
def test_unfinished_by_rule(partial, reason):
    assert unfinished(partial) == reason


@pytest.mark.parametrize("partial", [
    "帮我查一下订单S O二零二六一零一九一零一",  # the whole order number: nothing more to wait for
    "订单号是二零二六一零一九一零一", "我的手机号是一三八一二三四五六七八", "嗯", "我要这个",
    "会员怎么取消自动续费", "我要退款", "SO二零二六一零一九一零一的快递到哪了", "退我一百九十八块",
    "能导出成MP3吗", "", "好的就是这样吧谢谢",
])
def test_finished_by_rule(partial):
    assert unfinished(partial) is None


def test_rule_detector_is_unsure_unless_the_text_is_unfinished():
    rule = RuleTurn()
    assert rule(silence(1.0), "我要退款").decision == UNSURE
    verdict = rule(silence(1.0), "订单号是二零二六")
    assert verdict.decision == INCOMPLETE and verdict.reason == "digits"


# ---------------------------------------------------------------- the endpointer asks
class FakeTurn:
    """Answers in order; records the audio it was asked about."""

    def __init__(self, *decisions):
        self.decisions, self.asked = list(decisions), []

    def __call__(self, audio: np.ndarray) -> Verdict:
        self.asked.append(len(audio))
        return Verdict(self.decisions.pop(0) if self.decisions else COMPLETE, reason="fake")


def ends(events):
    return [e for e in events if e.kind == "end"]


def test_complete_ends_at_the_probe_not_the_silence_threshold():
    turn = FakeTurn(COMPLETE)
    ep = Endpointer(EnergyVAD(), end_silence_ms=600, turn=turn, probe_ms=250, max_wait_ms=2000)
    end = ends(ep.push(np.concatenate([silence(0.5), tone(1.0), silence(2.5)])))[0]
    assert end.reason == "complete" and end.verdict["decision"] == COMPLETE
    assert 250 <= endpoint_wait_ms(end) < 250 + 2 * FRAME_MS
    assert len(turn.asked) == 1


def test_incomplete_waits_up_to_the_cap():
    turn = FakeTurn(INCOMPLETE)
    ep = Endpointer(EnergyVAD(), end_silence_ms=600, turn=turn, probe_ms=250, max_wait_ms=2000)
    end = ends(ep.push(np.concatenate([silence(0.5), tone(1.0), silence(2.5)])))[0]
    assert end.reason == "silence"
    assert 2000 <= endpoint_wait_ms(end) < 2000 + 2 * FRAME_MS


def test_unsure_falls_back_to_the_silence_threshold():
    ep = Endpointer(EnergyVAD(), end_silence_ms=600, turn=FakeTurn(UNSURE), probe_ms=250, max_wait_ms=2000)
    end = ends(ep.push(np.concatenate([silence(0.5), tone(1.0), silence(2.5)])))[0]
    assert end.reason == "silence" and 600 <= endpoint_wait_ms(end) < 600 + 2 * FRAME_MS


def test_a_pause_judged_unfinished_keeps_the_utterance_whole():
    # "我的订单号是……" 1 s pause "……二零二六一零一九一零一": one utterance, asked at each pause.
    turn = FakeTurn(INCOMPLETE, COMPLETE)
    ep = Endpointer(EnergyVAD(), end_silence_ms=600, turn=turn, probe_ms=250, max_wait_ms=2000)
    events = ep.push(np.concatenate([silence(0.5), tone(1.0), silence(1.0), tone(1.0), silence(2.5)]))
    assert [e.kind for e in events].count("start") == 1 and len(ends(events)) == 1
    assert ends(events)[0].reason == "complete"
    assert len(turn.asked) == 2 and turn.asked[1] > turn.asked[0]  # the whole turn, both halves


def test_the_detector_sees_at_most_eight_seconds():
    turn = FakeTurn(COMPLETE)
    ep = Endpointer(EnergyVAD(), turn=turn, probe_ms=250, max_wait_ms=2000, max_utterance_ms=20_000)
    ep.push(np.concatenate([silence(0.5), tone(12.0), silence(1.0)]))
    assert turn.asked and turn.asked[0] <= 8 * SAMPLE_RATE


def test_without_a_detector_nothing_changes():
    ep = Endpointer(EnergyVAD(), end_silence_ms=600)
    end = ends(ep.push(np.concatenate([silence(0.5), tone(1.0), silence(1.0)])))[0]
    assert end.reason == "silence" and end.verdict is None


def test_a_failing_detector_falls_back_to_silence(caplog):
    def broken(audio):
        raise RuntimeError("model gone")

    ep = Endpointer(EnergyVAD(), end_silence_ms=600, turn=broken, probe_ms=250, max_wait_ms=2000)
    end = ends(ep.push(np.concatenate([silence(0.5), tone(1.0), silence(2.5)])))[0]
    assert end.reason == "silence" and 600 <= endpoint_wait_ms(end) < 600 + 2 * FRAME_MS
    assert "turn detector failed" in caplog.text


# ---------------------------------------------------------------- Smart Turn (fake ONNX session)
def test_log_mel_matches_whisper_shape_and_left_pads():
    audio = (np.random.default_rng(1).standard_normal(SAMPLE_RATE * 2) * 3000).astype(np.int16)
    features = log_mel(audio)
    assert features.shape == (80, 800) and features.dtype == np.float32
    assert np.all(np.isfinite(features))
    # Two seconds of audio sit at the end of the 8-second window: the start is the padding's floor.
    assert features[:, :400].std(axis=1).max() < 1e-3 < features[:, -100:].std(axis=1).min()
    long = np.concatenate([silence(9.0), audio])  # longer than 8 s: the beginning is dropped
    assert np.allclose(log_mel(long), log_mel(np.concatenate([silence(6.0), audio])), atol=1e-4)


class FakeSession:
    def __init__(self, probability):
        self.probability, self.shapes = probability, []

    def get_inputs(self):
        class Input:
            name = "input_features"
        return [Input()]

    def run(self, outputs, feeds):
        self.shapes.append(feeds["input_features"].shape)
        return [np.array([[self.probability]], dtype=np.float32)]


@pytest.mark.parametrize("probability,decision", [(0.9, COMPLETE), (0.2, INCOMPLETE)])
def test_smart_turn_thresholds_the_probability(probability, decision):
    session = FakeSession(probability)
    smart = SmartTurn(session=session, threshold=0.5)
    verdict = smart(tone(1.0), "我要退款")
    assert verdict.decision == decision and verdict.probability == pytest.approx(probability)
    assert session.shapes == [(1, 80, 800)] and verdict.reason == "smart_turn" and verdict.ms >= 0


def test_the_rule_overrules_smart_turn_on_digits():
    # The model hears falling intonation after "二零二六"; the customer is in the middle of the number.
    smart = SmartTurn(session=FakeSession(0.95), threshold=0.5)
    verdict = smart(tone(1.0), "订单号是S O二零二六")
    assert verdict.decision == INCOMPLETE and verdict.reason == "digits"


def test_detector_from_settings(monkeypatch):
    s = Settings()
    assert build_turn_detector(s) is None  # silence only: the default until held-out numbers say otherwise
    s.voice_endpointing = "rules"
    assert isinstance(build_turn_detector(s), RuleTurn)
    s.voice_endpointing = "smart_turn"
    monkeypatch.setattr("app.voice.turn.SmartTurn.load_session", staticmethod(lambda path: FakeSession(0.9)))
    smart = build_turn_detector(s)
    assert isinstance(smart, SmartTurn) and smart.threshold == s.voice_turn_threshold
    s.voice_endpointing = "telepathy"
    with pytest.raises(ValueError):
        build_turn_detector(s)


# ---------------------------------------------------------------- the session: take an answer back
class SlowTTS(ToneTTS):
    """First audio after ``delay`` seconds: the window in which the customer can still go on."""

    def __init__(self, delay: float):
        super().__init__(ms_per_char=20)
        self.delay = delay

    def synthesize(self, text):
        time.sleep(self.delay)
        yield from super().synthesize(text)


@pytest.fixture()
def voice(client, login):
    asr = ScriptedASR()
    headers = login("customer@echo.demo")
    token = headers["Authorization"].split(" ", 1)[1]
    conversation = client.post("/api/conversations", json={}, headers=headers).json()["id"]

    class Voice:
        def __init__(self):
            self.asr, self.conversation, self.client, self.token = asr, conversation, client, token

        def connect(self, tts=None, turn=None, asr=None):
            set_engines(VoiceEngines(asr or self.asr, tts or ToneTTS(ms_per_char=20), turn=turn))
            return _Connection(client, token, conversation)

    yield Voice()
    set_engines(None, None)


class _Connection:
    def __init__(self, client, token, conversation, mode="continuous"):
        self.client, self.token, self.conversation, self.mode = client, token, conversation, mode

    def __enter__(self):
        self.cm = self.client.websocket_connect("/api/voice/ws")
        ws = self.cm.__enter__()
        ws.send_json({"type": "auth", "token": self.token, "conversation_id": self.conversation, "mode": self.mode})
        assert json.loads(ws.receive()["text"])["type"] == "ready"
        return ws

    def __exit__(self, *exc):
        self.cm.__exit__(*exc)
        for worker in threading.enumerate():
            if worker.name == "loopdesk-voice-in":
                worker.join(timeout=30)


def voice_args(voice):
    return voice.client, voice.token, voice.conversation


def say(ws, speech=0.8, pause=0.9):
    audio = np.concatenate([tone(speech), silence(pause)])
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


def messages(conversation):
    with new_session() as db:
        return db.execute(select(Message).where(Message.conversation_id == conversation)
                          .order_by(Message.id)).scalars().all()


def runs(conversation):
    with new_session() as db:
        return db.execute(select(Run).where(Run.conversation_id == conversation)  # ids are random: by time
                          .order_by(Run.created_at)).scalars().all()


def test_speech_before_the_answer_plays_takes_it_back_and_merges(voice):
    # The endpointer cut "我想查一下订单" off; the customer goes on with the number before any audio played.
    voice.asr.transcripts += ["我想查一下订单", "S O 二零二六一零一九一零一"]
    with voice.connect(tts=SlowTTS(1.5)) as ws:
        say(ws)
        first = until(ws, "transcript")[-1]
        assert first["text"] == "我想查一下订单"
        say(ws)  # within the TTS delay: nothing has been played yet
        events = until(ws, "turn_saved")
    withdrawn = [e for e in events if e["type"] == "withdrawn"]
    assert len(withdrawn) == 1
    merged = [e for e in events if e["type"] == "transcript"][-1]
    assert merged["text"] == "我想查一下订单SO20261019101" and merged["merged"] is True
    users = [m.content for m in messages(voice.conversation) if m.role == "user"]
    assert users == ["我想查一下订单SO20261019101"]  # one request, as if the customer had not paused
    assert sum(m.role == "assistant" for m in messages(voice.conversation)) == 1
    statuses = [r.status for r in runs(voice.conversation)]
    assert statuses[0] == "withdrawn" and statuses[-1] == "completed"
    assert runs(voice.conversation)[0].error is None  # not a failure: nothing to alert on


def test_speech_after_the_answer_started_playing_is_a_barge_in(voice):
    voice.asr.transcripts += ["怎么取消自动续费", "好的知道了"]
    with voice.connect() as ws:
        say(ws)
        events = until(ws, "say")
        until(ws, "turn_end", events)  # audio of the first answer went out
        say(ws, pause=0.9)
        until(ws, "turn_saved", events)
        until(ws, "turn_saved", events)
    assert not any(e["type"] == "withdrawn" for e in events)
    users = [m.content for m in messages(voice.conversation) if m.role == "user"]
    assert users == ["怎么取消自动续费", "好的知道了"]


def agent_event(ws, name, events):
    while True:
        until(ws, "agent", events)
        if events[-1]["event"]["type"] == "tool_result" and events[-1]["event"]["name"] == name:
            return events


def test_a_run_that_changed_something_is_not_taken_back(voice):
    # "转人工" hands the conversation over (a write tool) before any audio: going on does not undo it.
    voice.asr.transcripts += ["转人工", "我的订单一直没到"]
    with voice.connect(tts=SlowTTS(1.5)) as ws:
        say(ws)
        events = agent_event(ws, "handoff_to_human", [])
        say(ws)  # TTS has not started, but the handoff happened
        until(ws, "turn_saved", events)
        until(ws, "turn_saved", events)
    assert not any(e["type"] == "withdrawn" for e in events)
    assert all(r.status != "withdrawn" for r in runs(voice.conversation))
    assert [m.content for m in messages(voice.conversation) if m.role == "user"] == ["转人工", "我的订单一直没到"]


def slow_model(monkeypatch, seconds):
    from app.services import get_services

    provider = get_services().provider
    chat = provider.chat

    def slow(*args, **kwargs):
        time.sleep(seconds)
        return chat(*args, **kwargs)

    monkeypatch.setattr(provider, "chat", slow)


def test_a_tool_that_changes_something_waits_while_the_customer_talks(voice, monkeypatch):
    # The model decides to open a ticket while the customer is still describing the fault: the ticket
    # waits, the customer's words are merged, and one ticket is opened with all of them.
    from app.models import Ticket

    slow_model(monkeypatch, 0.6)
    voice.asr.transcripts += ["我要报修", "App一打开就闪退"]
    with voice.connect() as ws:
        say(ws)
        events = until(ws, "transcript")
        say(ws)  # starts while the model is still deciding
        until(ws, "turn_saved", events)
    assert any(e["type"] == "withdrawn" for e in events)
    with new_session() as db:
        tickets = db.execute(select(Ticket).where(Ticket.conversation_id == voice.conversation)).scalars().all()
    assert len(tickets) == 1 and "闪退" in tickets[0].description
    assert [r.status for r in runs(voice.conversation)] == ["withdrawn", "completed"]


def test_a_withdrawn_run_stops_mid_way(voice, monkeypatch):
    # The model is slow; the run is still going when the customer goes on: it ends as withdrawn, not failed.
    slow_model(monkeypatch, 0.6)
    voice.asr.transcripts += ["会员退款多久能到账", "是年卡"]
    with voice.connect() as ws:
        say(ws)
        events = until(ws, "transcript")
        say(ws)
        until(ws, "turn_saved", events)
    first = runs(voice.conversation)[0]
    assert first.status == "withdrawn" and first.error is None
    assert [m.content for m in messages(voice.conversation) if m.role == "user"] == ["会员退款多久能到账是年卡"]


def test_a_cough_does_not_take_the_answer_back(voice):
    # Speech starts again but has no words (a cough, a "嗯" the ASR drops): the held answer is spoken.
    voice.asr.transcripts += ["怎么取消自动续费", ""]
    with voice.connect(tts=SlowTTS(1.0)) as ws:
        say(ws)
        events = until(ws, "transcript")
        say(ws)
        until(ws, "turn_saved", events)
    assert not any(e["type"] == "withdrawn" for e in events)
    assert any(e["type"] == "say" for e in events)
    assert [r.status for r in runs(voice.conversation)] == ["completed"]


def test_push_to_talk_is_never_taken_back(voice):
    voice.asr.transcripts += ["我想查一下订单", "S O 二零二六一零一九一零一"]
    set_engines(VoiceEngines(voice.asr, SlowTTS(1.0)))
    with _Connection(*voice_args(voice), mode="ptt") as ws:
        for _ in range(2):
            ws.send_json({"type": "ptt", "down": True})
            for start in range(0, SAMPLE_RATE // 2, 1600):
                ws.send_bytes(tone(0.1).tobytes())
            ws.send_json({"type": "ptt", "down": False})
        events = until(ws, "turn_saved")
        until(ws, "turn_saved", events)
    assert not any(e["type"] == "withdrawn" for e in events)
    assert len([m for m in messages(voice.conversation) if m.role == "user"]) == 2


def test_semantic_endpointing_in_the_session(voice):
    # The detector says the first pause is not the end: one utterance, one run, no withdrawal.
    voice.asr.transcripts += ["我的订单号是S O二零二六一零一九一零一"]
    turn = FakeTurn(INCOMPLETE, COMPLETE)
    with voice.connect(turn=lambda: (lambda audio, partial: turn(audio))) as ws:
        audio = np.concatenate([tone(0.8), silence(1.0), tone(0.8), silence(0.6)])
        for start in range(0, len(audio), 1600):
            ws.send_bytes(audio[start:start + 1600].tobytes())
        events = until(ws, "turn_saved")
    transcript = next(e for e in events if e["type"] == "transcript")
    assert transcript["endpoint"] == "complete" and transcript["text"] == "我的订单号是SO20261019101"
    assert 250 <= transcript["endpoint_wait_ms"] < 250 + 2 * FRAME_MS + 1
    assert len(turn.asked) == 2


def test_the_rule_sees_the_streaming_partial(voice):
    # Engines with partial results: the text so far reaches the detector.
    seen = []

    def detector(audio, partial):
        seen.append(partial)
        return Verdict(COMPLETE, reason="fake")

    class Partial(ScriptedASR):
        def stream(self, hotwords):
            inner = super().stream(hotwords)

            class S:
                def accept(self, pcm):
                    inner.accept(pcm)
                    return "订单号是二零二六"

                def finish(self):
                    return inner.finish()
            return S()

    with voice.connect(turn=lambda: detector, asr=Partial(["订单号是二零二六"])) as ws:
        say(ws, pause=0.6)
        until(ws, "turn_saved")
    assert seen and seen[-1] == "订单号是二零二六"
