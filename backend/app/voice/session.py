"""One voice conversation: audio in, the unchanged agent runtime in the middle, audio out.

    audio ─► endpointer ─► streaming ASR ─► ITN + hotwords ─► PII masked ─┐
                                                                       ▼
          confirmation state: a pending read-back takes "确认 / 取消" first
                                                                       ▼
    AgentRuntime.start(text) (same runtime, tools, approvals, evidence check, audit as text chat;
    the registry's money tools sit behind a spoken confirmation, voice/confirm.py)
          │ token events: sentences the output gate already released
          ▼
    sentence ─► speakable() ─► streaming TTS ─► audio out ─► client reports what it played

What the customer heard is what the conversation keeps: when they interrupt (barge-in), or a
sentence was never played, the assistant message is cut to the part that was played, and the
full generated text moves to meta.voice. The next turn's history then holds what was said, not
what the model wrote.

Threads: the transport feeds audio and control messages in order on one worker thread (VAD and
ASR); each turn runs the agent on its own thread; one TTS thread synthesises sentences in order.
``emit`` must be thread-safe (the WebSocket endpoint passes a queue's put).
"""

from __future__ import annotations

import logging
import queue
import re
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..admission import get_admission
from ..agent.runtime import AgentRuntime
from ..config import Settings
from ..guardrails.pii import mask_pii
from ..models import Conversation, Message, Tenant, User
from ..security import Principal, rate_limiter
from .confirm import (VOICE_CONFIRM_ERROR, Confirmations, classify_reply, gate_registry, readback_entities,
                      readback_refund)
from .endpoint import Endpointer, Event, endpoint_wait_ms
from .engines import ASRResult, VoiceEngines
from .itn import Entity, assess, extract_entities, itn
from .lexicon import Lexicon, lexicon_for
from .speakable import speakable, split_sentences

logger = logging.getLogger(__name__)

CANCELLED_TEXT = "好的，已取消，没有提交。还有什么可以帮您？"
REPEAT_TEXT = "好的，请您再说一遍订单号和金额。"
BUSY_TEXT = "您说得太快了，请稍后再试。"
UNCLEAR_TEXT = "抱歉，没有听清。确认请说“确认”，不对请说“取消”。"
READBACK_FAILED_TEXT = ("抱歉，语音播报出了问题，这笔退款没有提交。您可以在页面上发送“{order} 申请退款”，"
                        "或者说“转人工”。")
MAX_READBACKS = 2
INTERRUPTED_NOTE = "……"
NOTHING_HEARD = "（顾客在播报前打断了这条回复）"


@dataclass
class Transcript:
    text: str  # ITN'd, hotword-corrected, PII-masked: the only form that leaves this module
    entities: list[Entity] = field(default_factory=list)
    pii: list[str] = field(default_factory=list)
    fixes: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Sentence:
    id: int
    written: str
    sample_rate: int = 16_000
    spoken: str = ""
    redacted: list[str] = field(default_factory=list)
    samples: int = 0
    synthesized: bool = False
    played_ms: float = 0.0
    played: bool = False


@dataclass
class Turn:
    id: int
    sample_rate: int
    sentences: list[Sentence] = field(default_factory=list)
    message_id: int | None = None
    generated: str = ""
    cancelled: bool = False
    interrupted: bool = False
    run_done: bool = False
    finalized: bool = False
    marks: dict[str, float] = field(default_factory=dict)  # perf_counter seconds
    latency: dict[str, float] = field(default_factory=dict)  # ms, reported
    sent: threading.Event = field(default_factory=threading.Event)  # every sentence synthesised

    def add(self, written: str) -> Sentence:
        sentence = Sentence(len(self.sentences), written, self.sample_rate)
        self.sentences.append(sentence)
        return sentence

    def all_played(self) -> bool:
        return self.run_done and all(s.played for s in self.sentences if s.synthesized or not self.cancelled)

    def heard(self) -> tuple[str, bool]:
        """The written text of what was played, and whether playback was cut short."""
        parts, cut = [], False
        for s in self.sentences:
            if s.played:
                parts.append(s.written)
                continue
            duration = s.samples / self.sample_rate * 1000 if s.samples else 0
            if s.played_ms > 0 and duration > 0:
                n = int(len(s.written) * min(1.0, s.played_ms / duration))
                if n:
                    parts.append(s.written[:n])
            cut = True
            break
        return "".join(parts), cut


def _what_must_not_change(text: str, lexicon: Lexicon) -> tuple:
    normalized, _ = lexicon.correct(itn(text).text)  # "好华，会员" is not corrected, "好华会员" is
    return ({(e.kind, e.value) for e in extract_entities(normalized, lexicon.terms)},
            sorted(mask_pii(normalized).found))


def understand(result: ASRResult, lexicon: Lexicon, *, use_itn: bool = True, correct: bool = True) -> Transcript:
    """ASR output to what the agent receives: ITN, hotword correction, entity confidence, PII masked.
    The voice evaluation calls this too (with the switches off for its ablations). Added punctuation is
    only there to separate requests: when it changed an order number, an amount, a tenant term or what
    PII masking finds ("一三八。一二三四…" is no phone number any more), the text without it is used."""
    if result.unpunctuated is not None and (_what_must_not_change(result.text, lexicon)
                                            != _what_must_not_change(result.unpunctuated, lexicon)):
        logger.info("voice punctuation changed an entity or PII, using the transcript without it")
        result = replace(result, text=result.unpunctuated, unpunctuated=None)
    normalized = itn(result.text) if use_itn else None
    text = normalized.text if normalized else " ".join(result.text.split())
    fixes: list[tuple[str, str]] = []
    if correct:
        text, fixes = lexicon.correct(text)
    others = [v for v in result.passes.values() if v and v != result.text]
    entities = assess(extract_entities(text, lexicon.terms),
                      inferred_prefix=normalized.inferred_prefix if normalized else set(),
                      other_passes=others, char_probs=result.char_probs)
    pii = mask_pii(text)  # before anything is shown, stored or sent to a model
    return Transcript(pii.masked.strip(), entities, pii.found, fixes)


_LONG_DIGITS = re.compile(r"(?<![A-Za-z\d])\d{5,}(?!\d)")


def mask_partial(text: str) -> str:
    """A partial transcript as shown while the customer talks: a phone number half spoken is not yet
    eleven digits, so PII masking cannot recognise it; any long digit run outside an order number is hidden."""
    masked = mask_pii(itn(text).text).masked
    return _LONG_DIGITS.sub(lambda m: "*" * len(m.group(0)), masked)


def audio_frame(turn: int, sentence: int, pcm: np.ndarray) -> bytes:
    """Binary audio to the client: turn and sentence ids (uint16 each, little-endian), then int16 PCM."""
    return struct.pack("<HH", turn % 65536, sentence % 65536) + pcm.astype("<i2").tobytes()


class VoiceSession:
    def __init__(self, *, engines: VoiceEngines, services, settings: Settings, tenant_id: int, user_id: int,
                 conversation_id: int, emit: Callable[[Any], None], db_factory: Callable[[], Session],
                 mode: str = "continuous", assume_played: bool = False):
        self.engines, self.services, self.settings = engines, services, settings
        self.tenant_id, self.user_id, self.conversation_id = tenant_id, user_id, conversation_id
        self.emit, self.db_factory = emit, db_factory
        self.assume_played = assume_played  # evaluation: no client plays the audio back
        self.endpointer = Endpointer(engines.vad(), end_silence_ms=settings.voice_end_silence_ms,
                                     max_utterance_ms=int(settings.voice_max_utterance_seconds * 1000))
        self.endpointer.auto = mode == "continuous"
        self.mode = mode
        # While the agent talks, speech has to last longer to count: a cough or the agent's own voice
        # leaking into the microphone should not cut it off.
        self.start_frames = self.endpointer.start_frames
        self.barge_frames = max(self.start_frames, round(settings.voice_barge_in_ms / 32))
        self.final_lock = threading.Lock()
        self.confirmations = Confirmations()
        self.readback: dict | None = None  # a low-confidence order number or amount awaiting yes / no
        self.stream = None
        self.turn: Turn | None = None
        self.turn_seq = 0
        self.utterances: queue.Queue = queue.Queue()
        self.closed = False
        with self.db_factory() as db:
            tenant = db.get(Tenant, tenant_id)
            self.lexicon = lexicon_for(db, tenant) if settings.voice_hotwords else Lexicon([])
        self.hotwords = self.lexicon.terms
        self.tts_queue: queue.Queue = queue.Queue()
        self.tts_thread = threading.Thread(target=self._tts_worker, name="loopdesk-tts", daemon=True)
        self.tts_thread.start()
        self.turn_thread = threading.Thread(target=self._turns, name="loopdesk-voice-turns", daemon=True)
        self.turn_thread.start()

    # ================================================================ input (worker thread, in order)
    def handle_audio(self, data: bytes) -> None:
        pcm = np.frombuffer(data[: len(data) - len(data) % 2], dtype="<i2").astype(np.int16)
        self.endpointer.start_frames = self.barge_frames if self._speaking() else self.start_frames
        for event in self.endpointer.push(pcm):
            self._on_event(event)

    def handle_control(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "ptt":
            for event in (self.endpointer.press() if msg.get("down") else self.endpointer.release()):
                self._on_event(event)
        elif kind == "mode" and msg.get("mode") in ("ptt", "continuous"):
            self.mode = msg["mode"]
            self.endpointer.auto = self.mode == "continuous"
        elif kind == "played":
            self._played(msg)
        elif kind == "stop":
            self._barge_in("stop")

    def _on_event(self, event: Event) -> None:
        if event.kind == "start":
            if self._speaking():
                self._barge_in("speech")
            self.emit({"type": "vad", "speech": True})
            self.stream = self.engines.asr.stream(self.hotwords)
            self._feed(event.audio)
        elif event.kind == "audio":
            self._feed(event.audio)
        elif event.kind == "end" and self.stream is not None:
            self.emit({"type": "vad", "speech": False, "reason": event.reason})
            ended = time.perf_counter()
            stream, self.stream = self.stream, None
            result = stream.finish()
            self._utterance(result, event, ended)

    def _feed(self, pcm: np.ndarray) -> None:
        if self.stream is None or not len(pcm):
            return
        partial = self.stream.accept(pcm)
        if partial:
            self.emit({"type": "partial", "text": mask_partial(partial)})

    # ================================================================ understanding
    def understand(self, result: ASRResult) -> Transcript:
        return understand(result, self.lexicon)

    def _utterance(self, result: ASRResult, event: Event, ended: float) -> None:
        """Hand the utterance to the turn thread and return: this worker keeps reading audio (VAD,
        barge-in, playback reports) while a slow run is still answering the previous one."""
        self.utterances.put((result, event, ended))

    # ================================================================ turns (one thread, in order)
    def _turns(self) -> None:
        while (item := self.utterances.get()) is not None:
            result, event, ended = item
            try:
                self._settle_previous()  # the history the next run reads must hold what was heard
                transcript = self.understand(result)
                latency = {"endpoint_wait_ms": round(endpoint_wait_ms(event) if event.reason == "silence" else 0.0, 1),
                           "asr_final_ms": round(result.final_ms, 1)}
                self.emit({"type": "transcript", "text": transcript.text,
                           "entities": [e.to_dict() for e in transcript.entities], "pii": transcript.pii,
                           "hotword_fixes": [list(f) for f in transcript.fixes], "endpoint": event.reason, **latency})
                if not transcript.text:
                    self.emit({"type": "no_speech"})
                    continue
                self._decide(transcript, latency, ended)
            except Exception as exc:  # report, keep the session alive
                logger.exception("voice turn failed")
                self.emit({"type": "error", "message": f"{type(exc).__name__}"})

    def _decide(self, transcript: Transcript, latency: dict, ended: float) -> None:
        reply = classify_reply(transcript.text)
        self.confirmations.new_turn()  # a confirmation the last run did not use does not carry over
        pending = self.confirmations.pending
        if pending is not None:
            unclear = reply is None and len(transcript.text) <= 8 and not any(e.kind == "order_id" for e in transcript.entities)
            if reply == "yes" or (unclear and not self._heard(pending.readback)):
                if not self._heard(pending.readback):
                    # Cut off before the amount, or never played: the customer has not heard what they agree to.
                    silent = pending.readback is None or pending.readback.samples == 0
                    if silent and pending.attempts >= MAX_READBACKS:  # TTS keeps failing: no loop, no money
                        self.confirmations.decline()
                        return self._prompt(READBACK_FAILED_TEXT.format(order=pending.order_id), transcript,
                                            "readback_failed", latency, ended)
                    pending.attempts += silent  # a customer who talks over the amount just hears it again
                    return self._prompt(readback_refund(pending.order_id, pending.amount), transcript, "readback_again",
                                        latency, ended, pending=pending)
                pending = self.confirmations.accept()
                confirmation = self._record_confirmation(pending, transcript)
                return self.run_agent(f"确认：{pending.utterance}", latency, ended, confirmation=confirmation)
            if unclear:
                # "呃", "好了" after hearing the amount: not an answer, not a new request. Ask again; it stays pending.
                return self._prompt(UNCLEAR_TEXT, transcript, "readback_unclear", latency, ended)
            self.confirmations.decline()
            if reply == "no":
                return self._prompt(CANCELLED_TEXT, transcript, "cancelled", latency, ended)
        elif self.readback is not None:
            readback, self.readback = self.readback, None
            if reply == "yes" and self._heard(readback["sentence"]):
                # The order number or amount was right. A refund still gets its own spoken confirmation:
                # this question never mentioned one (review, P1).
                return self.run_agent(readback["text"], latency, ended)
            if reply == "yes":
                self.readback = readback
                return self._prompt(readback["question"], transcript, "readback_again", latency, ended, entity=readback)
            if reply == "no":
                return self._prompt(REPEAT_TEXT, transcript, "repeat", latency, ended)
        unsure = [e for e in transcript.entities
                  if e.kind in ("order_id", "amount") and e.confidence < self.settings.voice_readback_threshold]
        if unsure:
            order = next((e.value for e in transcript.entities if e.kind == "order_id"), None)
            amount = next((e.value for e in transcript.entities if e.kind == "amount"), None)
            self.readback = {"text": transcript.text, "question": readback_entities(order, amount), "sentence": None}
            return self._prompt(self.readback["question"], transcript, "readback", latency, ended, entity=self.readback)
        self.run_agent(transcript.text, latency, ended)

    @staticmethod
    def _heard(sentence: "Sentence | None") -> bool:
        """Played to the end, as the client reported (or assumed, in the evaluation)."""
        if sentence is None or sentence.samples == 0:  # no audio (TTS failed): nothing was heard, whatever is reported
            return False
        duration = sentence.samples / max(1, sentence.sample_rate) * 1000
        return sentence.played or sentence.played_ms >= 0.95 * duration

    def _record_confirmation(self, pending, transcript: Transcript) -> dict:
        """Consent to move money, kept like a reviewer's decision: who, what was read back, whether it
        was heard in full, what they said. Committed before the run, whatever the run then does."""
        sentence = pending.readback
        detail = {"tool": pending.tool, "order_id": pending.order_id, "amount": pending.amount,
                  "readback": sentence.written if sentence else "", "readback_heard": True,
                  "readback_played_ms": round(sentence.played_ms, 1) if sentence else 0.0, "reply": transcript.text}
        with self.db_factory() as db:
            user, tenant = db.get(User, self.user_id), db.get(Tenant, self.tenant_id)
            audit.record(db, Principal(user, tenant), "voice.confirm", f"conversation:{self.conversation_id}", **detail)
            db.commit()
        return detail

    def _new_turn(self, latency: dict, ended: float) -> Turn:
        self.turn_seq += 1
        turn = Turn(self.turn_seq, self.engines.tts.sample_rate, latency=dict(latency))
        turn.marks["ended"] = ended
        self.turn = turn
        return turn

    def _prompt(self, text: str, transcript: Transcript, kind: str, latency: dict, ended: float, *,
                pending=None, entity: dict | None = None) -> None:
        """A turn the voice channel answers itself (a read-back, a cancellation): no model, no run.
        Kept in the conversation as a system line, outside the model's history."""
        turn = self._new_turn(latency, ended)
        with self.db_factory() as db:
            message = Message(tenant_id=self.tenant_id, conversation_id=self.conversation_id, role="system",
                              content=text, meta={"voice": {"kind": kind, "customer": transcript.text}})
            db.add(message)
            db.commit()
            turn.message_id = message.id
        turn.generated = text
        self.emit({"type": "prompt", "kind": kind, "text": text, "message_id": turn.message_id})
        sentences = self._say_all(turn, text)
        if pending is not None and sentences:
            pending.readback = sentences[0]  # the sentence with the amount: the yes is checked against it
        if entity is not None and sentences:
            entity["sentence"] = sentences[-1]
        turn.run_done = True
        self._turn_sent(turn)

    def run_agent(self, text: str, latency: dict, ended: float, confirmation: dict | None = None) -> Turn:
        turn = self._new_turn(latency, ended)
        try:
            rate_limiter.check(f"chat:{self.user_id}", self.settings.rate_limit_per_minute)
        except HTTPException:
            self._say(turn, BUSY_TEXT)
            turn.run_done = True
            self._turn_sent(turn)
            return turn
        # A voice turn is a run like a chat message: it takes a slot from the same shares (N-181).
        admission = get_admission()
        try:
            slot = admission.enter(self.tenant_id, self.user_id)
        except HTTPException as exc:
            self._say(turn, exc.detail)
            turn.run_done = True
            self._turn_sent(turn)
            return turn
        try:
            return self._run_admitted(turn, text, confirmation)
        finally:
            admission.leave(slot)

    def _run_admitted(self, turn: Turn, text: str, confirmation: dict | None) -> Turn:
        db = self.db_factory()
        pending_before = self.confirmations.pending
        streamed, blocked, replaced, final = False, False, None, None
        try:
            s = self.services
            runtime = AgentRuntime(db, self.settings, s.provider, s.embedder,
                                   gate_registry(s.registry, self.confirmations, lambda: text), s.breaker, s.commerce,
                                   reranker=getattr(s, "reranker", None))
            turn.marks["run_started"] = time.perf_counter()
            for event in runtime.start(tenant=db.get(Tenant, self.tenant_id), user=db.get(User, self.user_id),
                                       text=text, conversation=db.get(Conversation, self.conversation_id)):
                self.emit({"type": "agent", "event": event})
                kind = event["type"]
                if kind == "token" and not blocked:  # whole sentences: the output gate releases no less
                    for sentence in split_sentences(event["text"]):
                        streamed = True
                        self._say(turn, sentence)
                elif kind == "tool_result" and event.get("is_error") and \
                        (event.get("output") or {}).get("error") == VOICE_CONFIRM_ERROR:
                    blocked = True  # what the model says next is replaced by the read-back
                elif kind == "answer_replaced":
                    replaced = event["text"]
                elif kind == "final":
                    final = event
        finally:
            db.close()
        if final:
            turn.message_id, turn.generated = final.get("message_id"), final.get("text") or ""
        if confirmation is not None:
            self._mark_confirmed(turn.message_id, confirmation)
        pending = self.confirmations.pending
        if blocked and pending is not None and pending is not pending_before:
            pending.readback = self._say_all(turn, readback_refund(pending.order_id, pending.amount))[0]
        elif replaced is not None:  # what was spoken before the gate stopped stands; then the replacement
            self._say_all(turn, replaced)
        elif not streamed and final:
            self._say_all(turn, turn.generated)  # strict evidence mode or the output judge: no stream
        turn.run_done = True
        self._turn_sent(turn)
        return turn

    def _mark_confirmed(self, answer_id: int | None, confirmation: dict) -> None:
        """The user message this run stored says "确认：<request>"; what the customer actually said is kept with it."""
        if answer_id is None:  # the run ended before saving its answer: which user message is ours is unknown
            return
        with self.db_factory() as db:
            query = select(Message).where(Message.conversation_id == self.conversation_id, Message.role == "user",
                                          Message.id < answer_id)
            message = db.execute(query.order_by(Message.id.desc()).limit(1)).scalar_one_or_none()
            if message is not None:
                message.meta = {**(message.meta or {}), "voice": {"reply": confirmation["reply"],
                                                                  "confirmed": confirmation}}
                db.commit()

    def _say_all(self, turn: Turn, text: str) -> list[Sentence]:
        return [s for s in (self._say(turn, sentence) for sentence in split_sentences(text)) if s is not None]

    def _say(self, turn: Turn, written: str) -> "Sentence | None":
        if not written.strip():
            return None
        sentence = turn.add(written.strip())
        turn.marks.setdefault("first_sentence", time.perf_counter())
        self.tts_queue.put((turn, sentence))
        return sentence

    def _turn_sent(self, turn: Turn) -> None:
        self.tts_queue.put((turn, None))  # after its last sentence: report the turn's latency

    # ================================================================ TTS (its own thread, in order)
    def _tts_worker(self) -> None:
        while (item := self.tts_queue.get()) is not None:
            turn, sentence = item
            try:
                if sentence is None:
                    self._report(turn)
                elif not turn.cancelled:
                    self._synthesize(turn, sentence)
            except Exception:
                logger.exception("TTS failed")
                self.emit({"type": "error", "message": "TTS 失败"})

    def _synthesize(self, turn: Turn, sentence: Sentence) -> None:
        speech = speakable(sentence.written)
        sentence.spoken, sentence.redacted = speech.text, speech.redacted
        self.emit({"type": "say", "turn": turn.id, "id": sentence.id, "text": speech.text, "written": sentence.written,
                   "sample_rate": turn.sample_rate})
        started = time.perf_counter()
        for chunk in self.engines.tts.synthesize(speech.text):
            if turn.cancelled:
                break
            if "first_audio" not in turn.marks:
                turn.marks["first_audio"] = time.perf_counter()
                turn.latency["tts_first_packet_ms"] = round((turn.marks["first_audio"] - started) * 1000, 1)
            sentence.samples += len(chunk)
            self.emit(audio_frame(turn.id, sentence.id, chunk))
        sentence.synthesized = True
        self.emit({"type": "say_end", "turn": turn.id, "id": sentence.id, "samples": sentence.samples})
        if self.assume_played and not turn.cancelled:
            sentence.played = True

    def _report(self, turn: Turn) -> None:
        m, lat = turn.marks, turn.latency
        if "run_started" in m and "first_sentence" in m:
            lat["llm_first_sentence_ms"] = round((m["first_sentence"] - m["run_started"]) * 1000, 1)
        if "first_audio" in m:
            lat["first_audio_after_endpoint_ms"] = round((m["first_audio"] - m["ended"]) * 1000, 1)
        self.emit({"type": "turn_end", "turn": turn.id, "message_id": turn.message_id, "latency": lat})
        turn.sent.set()
        if self.assume_played or turn.all_played():
            self._finalize(turn)

    # ================================================================ playback and barge-in
    def _speaking(self) -> bool:
        turn = self.turn
        return turn is not None and not turn.cancelled and not turn.finalized and \
            (not turn.run_done or any(not s.played for s in turn.sentences))

    def _barge_in(self, reason: str) -> None:
        turn = self.turn
        if turn is None or turn.cancelled or turn.finalized:
            return
        turn.cancelled, turn.interrupted = True, True
        self.emit({"type": "barge_in", "turn": turn.id, "reason": reason})

    def _played(self, msg: dict) -> None:
        turn = self.turn
        if turn is None or msg.get("turn") != turn.id:
            return
        index = msg.get("id")
        if not isinstance(index, int) or not 0 <= index < len(turn.sentences):
            return
        sentence = turn.sentences[index]
        sentence.played_ms = max(sentence.played_ms, float(msg.get("ms") or 0))
        if msg.get("done"):
            sentence.played = True
        if turn.all_played() and not turn.cancelled:
            self._finalize(turn)

    def _settle_previous(self, superseded: bool = True) -> None:
        """Before the next utterance is answered: the last turn's message keeps what was heard. A newer
        utterance supersedes an answer still playing: that is an interruption, and the client is told to
        stop it. On close nothing supersedes it, and delivery stays unknown."""
        turn = self.turn
        if superseded and turn is not None and not self.assume_played and self._speaking():
            self._barge_in("superseded")
        if turn is not None and self.assume_played:
            turn.sent.wait(timeout=120)
        if turn is not None and not turn.finalized:
            # Sentences still queued for synthesis are never played now.
            turn.cancelled = True
            self._finalize(turn)

    def _finalize(self, turn: Turn) -> None:
        """Keep what the customer heard as the message (see the module docstring)."""
        with self.final_lock:
            if turn.finalized:
                return
            turn.finalized = True
        heard, cut = turn.heard()
        # Only a barge-in (talking over it, the stop button) is an interruption. A page closed or a network
        # dropped mid-answer says nothing about what was heard: the answer is kept whole and marked unknown.
        interrupted = turn.interrupted
        delivery = "interrupted" if interrupted else ("unknown" if cut else "complete")
        voice = {"channel": "voice", "spoken": heard, "interrupted": interrupted, "delivery": delivery,
                 "latency": turn.latency, "redacted": sorted({k for s in turn.sentences for k in s.redacted})}
        if turn.message_id is not None:
            with self.db_factory() as db:
                message = db.get(Message, turn.message_id)
                if message is not None:
                    meta = dict(message.meta or {})
                    previous = dict(meta.get("voice") or {})
                    if delivery != "unknown" and heard != message.content:
                        voice["generated"] = message.content
                        message.content = (heard + INTERRUPTED_NOTE) if (interrupted and heard) else (heard or NOTHING_HEARD)
                    meta["voice"] = {**previous, **voice}
                    message.meta = meta
                    db.commit()
        self.emit({"type": "turn_saved", "turn": turn.id, "message_id": turn.message_id, "heard": heard,
                   "interrupted": interrupted})

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.utterances.put(None)  # turns already queued are answered first; the run always completes
        self.turn_thread.join(timeout=self.settings.run_deadline_seconds + 30)
        if self.turn_thread.is_alive():
            logger.warning("voice session %s closed with a turn still running", self.conversation_id)
        self._settle_previous(superseded=False)
        self.tts_queue.put(None)
        self.tts_thread.join(timeout=10)
