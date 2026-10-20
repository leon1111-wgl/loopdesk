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
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

SAMPLE_RATE = 16_000
FRAME = 512  # 32 ms: Silero VAD's frame at 16 kHz; the energy detector uses the same


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
    reason: str = ""  # end: silence | max_length | released
    speech_end: int = 0  # end: sample index (stream time) just after the last speech frame
    at: int = 0  # sample index (stream time) where the event happened


class Endpointer:
    def __init__(self, vad: VAD, *, end_silence_ms: int = 600, start_ms: int = 96, preroll_ms: int = 320,
                 max_utterance_ms: int = 20_000, threshold: float = 0.5):
        self.vad, self.threshold = vad, threshold
        self.end_frames = max(1, round(end_silence_ms / 1000 * SAMPLE_RATE / FRAME))
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
                self.preroll.clear()
                self.utterance_samples = len(head)
                self.last_speech_end = self.position
                return [Event("start", head, at=self.position - len(head))]
            return []
        self.utterance_samples += FRAME
        events = [Event("audio", frame, at=start)]
        if voiced:
            self.silent_run = 0
            self.last_speech_end = self.position
        else:
            self.silent_run += 1
        if self.silent_run >= self.end_frames:
            events += self._end("silence")
        elif self.utterance_samples >= self.max_samples:
            events += self._end("max_length")
        return events

    def _end(self, reason: str) -> list[Event]:
        self.in_speech, self.manual = False, False
        self.silent_run = self.voiced_run = 0
        return [Event("end", reason=reason, speech_end=self.last_speech_end, at=self.position)]


def endpoint_wait_ms(event: Event) -> float:
    """Audio time between the end of speech and the decision that the utterance is over."""
    return (event.at - event.speech_end) / SAMPLE_RATE * 1000
