"""Voice activity detection and endpointing, in audio time.

The endpointer counts samples, not wall-clock time: "600 ms of silence" means 600 ms of the
customer's audio, however fast it arrives. That keeps the behaviour the same in production
(real time), in tests (instant) and in the evaluation (faster than real time), and makes the
endpoint wait a property of the audio that can be measured offline.

    idle ──(speech ≥ start_ms)──► speech ──(silence ≥ end_silence_ms)──► end
      ▲  keeps preroll_ms of audio      │ or max_utterance_ms reached
      └─────────────────────────────────┘

Push-to-talk skips the detector: the button press starts an utterance and the release ends it,
with no silence to wait for.

Semantic endpointing (``turn``, voice/turn.py): at a short pause (``probe_ms``) the endpointer asks
whether the customer is done, once per pause, with the turn's audio so far (at most 8 s). "complete"
ends the utterance there; "incomplete" keeps listening up to ``max_wait_ms`` of silence; "unsure" (or a
detector that fails) falls back to ``end_silence_ms``. Speech before the end resets the pause.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
FRAME = 512  # 32 ms: Silero VAD's frame at 16 kHz; the energy detector uses the same
TURN_WINDOW = 8 * SAMPLE_RATE  # the turn detector reads at most the last 8 s


class VAD(Protocol):
    def __call__(self, frame: np.ndarray) -> float:
        """Speech probability of one FRAME of int16 audio."""


class EnergyVAD:
    """Level above an adaptive noise floor. No model, no dependency: the default, and what the
    tests use. It cannot tell speech from other loud sound (music, a TV), which a model VAD can."""

    def __init__(self, min_db: float = -45.0, margin_db: float = 10.0):
        self.min_db, self.margin_db = min_db, margin_db
        self.floor = -70.0

    def __call__(self, frame: np.ndarray) -> float:
        rms = math.sqrt(float(np.mean(np.square(frame.astype(np.float64))))) if len(frame) else 0.0
        level = 20 * math.log10(max(rms, 1e-9) / 32768)
        speech = level > max(self.min_db, self.floor + self.margin_db)
        # The floor follows quiet frames at once and loud ones slowly, so speech does not raise it.
        self.floor = level if level < self.floor else self.floor + (0.02 if speech else 0.2)
        return 1.0 if speech else 0.0


class SileroVAD:
    """Silero VAD (ONNX). Optional: needs the ``silero-vad`` package (requirements-voice.txt)."""

    def __init__(self):
        import torch  # noqa: F401  (the package's wrapper takes tensors)
        from silero_vad import load_silero_vad

        self._model = load_silero_vad(onnx=True)

    def __call__(self, frame: np.ndarray) -> float:
        import torch

        return float(self._model(torch.from_numpy(frame.astype(np.float32) / 32768), SAMPLE_RATE).item())


@dataclass
class Event:
    kind: str  # start | audio | end
    audio: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int16))
    reason: str = ""  # end: silence | complete (the turn detector) | max_length | released
    speech_end: int = 0  # end: sample index (stream time) just after the last speech frame
    at: int = 0  # sample index (stream time) where the event happened
    verdict: dict | None = None  # end: the turn detector's last answer in this utterance, if it was asked


class Endpointer:
    def __init__(self, vad: VAD, *, end_silence_ms: int = 600, start_ms: int = 96, preroll_ms: int = 320,
                 max_utterance_ms: int = 20_000, threshold: float = 0.5,
                 turn: Callable[[np.ndarray], object] | None = None, probe_ms: int = 250, max_wait_ms: int = 2000):
        self.vad, self.threshold = vad, threshold
        self.end_frames = max(1, round(end_silence_ms / 1000 * SAMPLE_RATE / FRAME))
        self.turn = turn  # audio -> Verdict (voice/turn.py); None: silence only
        self.probe_frames = max(1, round(probe_ms / 1000 * SAMPLE_RATE / FRAME))
        self.cap_frames = max(self.probe_frames, math.ceil(max_wait_ms / 1000 * SAMPLE_RATE / FRAME))
        self.wait_frames = self.end_frames  # this pause's silence budget, as the detector last decided
        self.verdict: dict | None = None
        self.recent: deque[np.ndarray] = deque(maxlen=TURN_WINDOW // FRAME + 1)  # the turn's audio, for the detector
        self.start_frames = max(1, round(start_ms / 1000 * SAMPLE_RATE / FRAME))
        self.max_samples = int(max_utterance_ms / 1000 * SAMPLE_RATE)
        self.preroll: deque[np.ndarray] = deque(maxlen=max(1, round(preroll_ms / 1000 * SAMPLE_RATE / FRAME)))
        self.buffer = np.zeros(0, dtype=np.int16)
        self.position = 0  # samples consumed so far (stream time)
        self.in_speech = False
        self.manual = False  # push-to-talk: the button decides, not the detector
        self.auto = True  # continuous mode: the detector starts utterances; off in push-to-talk mode
        self.voiced_run = 0
        self.silent_run = 0
        self.utterance_samples = 0
        self.last_speech_end = 0

    def press(self) -> list[Event]:
        """Push-to-talk pressed: an utterance starts now (any utterance in progress ends first)."""
        events = self._end("released") if self.in_speech else []
        self.manual, self.in_speech = True, True
        self.utterance_samples, self.silent_run = 0, 0
        self.verdict = None
        self.last_speech_end = self.position
        head = np.concatenate(list(self.preroll)) if self.preroll else np.zeros(0, dtype=np.int16)
        self.preroll.clear()
        return events + [Event("start", head, at=self.position)]

    def release(self) -> list[Event]:
        if not self.in_speech:
            self.manual = False
            return []
        events = []
        if len(self.buffer):  # the partial frame still waiting is part of the utterance
            events.append(Event("audio", self.buffer, at=self.position))
            self.position += len(self.buffer)
            self.buffer = np.zeros(0, dtype=np.int16)
        self.last_speech_end = self.position
        return events + self._end("released")

    def push(self, pcm: np.ndarray) -> list[Event]:
        self.buffer = np.concatenate([self.buffer, pcm.astype(np.int16)])
        events: list[Event] = []
        while len(self.buffer) >= FRAME:
            frame, self.buffer = self.buffer[:FRAME], self.buffer[FRAME:]
            events.extend(self._frame(frame))
        return events

    def _frame(self, frame: np.ndarray) -> list[Event]:
        voiced = self.vad(frame) >= self.threshold
        start = self.position
        self.position += FRAME
        if self.manual:
            self.utterance_samples += FRAME
            if voiced:
                self.last_speech_end = self.position
            events = [Event("audio", frame, at=start)]
            if self.utterance_samples >= self.max_samples:
                events += self._end("max_length")
            return events
        if not self.in_speech:
            self.preroll.append(frame)
            if not self.auto:
                return []
            self.voiced_run = self.voiced_run + 1 if voiced else 0
            if self.voiced_run >= self.start_frames:
                self.in_speech, self.silent_run, self.voiced_run = True, 0, 0
                head = np.concatenate(list(self.preroll))
                self.recent.clear()
                self.recent.extend(self.preroll)
                self.preroll.clear()
                self.wait_frames, self.verdict = self.end_frames, None
                self.utterance_samples = len(head)
                self.last_speech_end = self.position
                return [Event("start", head, at=self.position - len(head))]
            return []
        self.utterance_samples += FRAME
        self.recent.append(frame)
        events = [Event("audio", frame, at=start)]
        if voiced:
            self.silent_run = 0
            self.last_speech_end = self.position
            self.wait_frames = self.end_frames  # a new pause will be asked about afresh
        else:
            self.silent_run += 1
            if self.turn is not None and self.silent_run == self.probe_frames:
                decision = self._ask()
                if decision == "complete":
                    return events + self._end("complete")
                self.wait_frames = self.cap_frames if decision == "incomplete" else max(self.end_frames, self.probe_frames)
        if self.silent_run >= self.wait_frames:
            events += self._end("silence")
        elif self.utterance_samples >= self.max_samples:
            events += self._end("max_length")
        return events

    def _ask(self) -> str:
        """The turn detector's decision on the audio so far; "unsure" when it fails (the silence threshold
        still ends the utterance: a broken model must not leave the customer waiting forever)."""
        audio = np.concatenate(list(self.recent))[-TURN_WINDOW:]
        try:
            verdict = self.turn(audio)
        except Exception:
            logger.exception("turn detector failed; falling back to the silence threshold")
            self.verdict = {"decision": "unsure", "reason": "error"}
            return "unsure"
        self.verdict = verdict.to_dict() if hasattr(verdict, "to_dict") else dict(verdict)
        return self.verdict["decision"]

    def _end(self, reason: str) -> list[Event]:
        self.in_speech, self.manual = False, False
        self.silent_run = self.voiced_run = 0
        verdict, self.verdict = self.verdict, None
        self.wait_frames = self.end_frames
        return [Event("end", reason=reason, speech_end=self.last_speech_end, at=self.position, verdict=verdict)]


def endpoint_wait_ms(event: Event) -> float:
    """Audio time between the end of speech and the decision that the utterance is over."""
    return (event.at - event.speech_end) / SAMPLE_RATE * 1000
