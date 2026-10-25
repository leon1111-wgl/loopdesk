"""Has the customer finished their turn? Asked when the VAD hears a short pause, instead of only
counting silence.

A fixed silence threshold has to choose between cutting people off and making them wait: real
speakers pause mid-sentence for longer than TTS does (600 ms of silence cut 0.2% of synthetic
utterances and 13.7% / 53.3% of AISHELL-1 / FLEURS ones, docs/EVALUATION.md). So the endpointer asks
at a short pause (``probe_ms``) and gets one of three answers:

    complete    end the utterance now (the wait is the probe, ~250 ms)
    incomplete  keep listening, up to ``max_wait_ms`` of silence
    unsure      fall back to the silence threshold (``end_silence_ms``)

Two detectors, combinable:
- ``RuleTurn``: the streaming transcript so far ends in digits (an order or phone number being read
  out) or a connective (然后, 还有, 就是, 呃 …): incomplete. Otherwise it cannot tell: unsure.
- ``SmartTurn``: Smart Turn v3 (pipecat-ai/smart-turn, BSD-2-Clause), a Whisper-tiny encoder with a
  classifier, ~8 MB int8 ONNX, that reads the last 8 s of the turn's audio (intonation, a trailing
  word) and returns P(complete). The rule overrules it: a falling tone after "二零二六" is still the
  middle of an order number.

Endpointing is ``silence`` (no detector) by default; ``rules`` and ``smart_turn`` are opt-in
(LOOPDESK_VOICE_ENDPOINTING) until held-out numbers support a change. A mistake either way is
recoverable: when the customer goes on before the answer starts playing, the session takes the
answer back and merges the two transcripts (session.py).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass

import numpy as np

from ..config import Settings
from .itn import itn

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
WINDOW_SECONDS = 8  # Smart Turn reads the last 8 s of the turn

COMPLETE, INCOMPLETE, UNSURE = "complete", "incomplete", "unsure"


@dataclass
class Verdict:
    decision: str  # complete | incomplete | unsure
    probability: float | None = None  # Smart Turn's P(complete); None for the rule alone
    reason: str = ""  # smart_turn | digits | connective | rule | error
    ms: float = 0.0  # compute time

    def to_dict(self) -> dict:
        out = asdict(self)
        if self.probability is not None:
            out["probability"] = round(self.probability, 3)
        out["ms"] = round(self.ms, 1)
        return out


# ---------------------------------------------------------------- the rule
# Digits at the very end: "订单号是S O二零二六一零", "手机号一三八一二", "SO 2026 1015". Two or more, so
# "退一百" or "第一" do not count; spaces and commas between them are how engines write a pause. A number
# that is already whole (an order number SO + 11 digits, an 11-digit mobile number) ends nothing early.
_TRAILING_DIGITS = re.compile(r"[零〇一二三四五六七八九幺两0-9](?:[\s,，、]*[零〇一二三四五六七八九幺两0-9])+[\s,，、]*$")
# Words that join to what comes next; Mandarin and Cantonese, Simplified and Traditional. Kept short on
# purpose: a word here can only make the endpointer wait longer, but "我要这个" is a whole sentence, so
# 这个 / 那个 count only after 就是; a hesitation (呃) counts anywhere, but 嗯 only after other words:
# a lone "嗯" is an answer.
CONNECTIVES = ("然后", "然後", "还有", "還有", "就是", "就係", "而且", "但是", "可是", "不过", "不過", "还是", "還是",
               "或者", "另外", "因为", "因為", "所以", "如果", "比如", "就是那个", "就是那個", "就係嗰個",
               "同埋", "跟住", "仲有", "即係", "定係", "呃", "额")
FILLERS = ("嗯",)
_TAIL = r"[\s,，、.。…~]*$"
_TRAILING_CONNECTIVE = re.compile(rf"(?:{'|'.join(sorted(map(re.escape, CONNECTIVES), key=len, reverse=True))}){_TAIL}")
_TRAILING_FILLER = re.compile(rf"(?<=\S)[\s,，、]*(?:{'|'.join(map(re.escape, FILLERS))}){_TAIL}")


def unfinished(partial: str) -> str | None:
    """Why the transcript so far cannot be the end of a turn ("digits", "connective"), or None."""
    text = (partial or "").strip()
    if not text:
        return None
    if _TRAILING_DIGITS.search(text):
        normalized = itn(text).text.rstrip(" ,，、.。")
        digits = re.search(r"\d+$", normalized)
        whole = re.search(r"SO\d{11}$", normalized, re.I) or (digits and re.fullmatch(r"1\d{10}", digits.group(0)))
        return None if whole else "digits"
    if _TRAILING_CONNECTIVE.search(text) or _TRAILING_FILLER.search(text):
        return "connective"
    return None


class RuleTurn:
    """The text rule alone: no model. Incomplete or unsure, never complete."""

    name = "rules"

    def __call__(self, audio: np.ndarray, partial: str) -> Verdict:
        reason = unfinished(partial)
        return Verdict(INCOMPLETE, reason=reason) if reason else Verdict(UNSURE, reason="rule")


# ---------------------------------------------------------------- Whisper log-mel, in numpy
def _hz_to_mel(freq: np.ndarray) -> np.ndarray:  # Slaney's scale, as librosa and transformers use it
    freq = np.asarray(freq, dtype=np.float64)
    mels = 3.0 * freq / 200.0
    log = freq >= 1000.0
    return np.where(log, 15.0 + np.log(np.maximum(freq, 1e-10) / 1000.0) * (27.0 / np.log(6.4)), mels)


def _mel_to_hz(mels: np.ndarray) -> np.ndarray:
    mels = np.asarray(mels, dtype=np.float64)
    freq = 200.0 * mels / 3.0
    return np.where(mels >= 15.0, 1000.0 * np.exp(np.log(6.4) / 27.0 * (mels - 15.0)), freq)


def _mel_filters(n_fft: int = 400, n_mels: int = 80, rate: int = SAMPLE_RATE) -> np.ndarray:
    """(n_fft // 2 + 1, n_mels) triangular filters, Slaney-normalised: Whisper's filter bank."""
    fft_freqs = np.linspace(0, rate // 2, n_fft // 2 + 1)
    filter_freqs = _mel_to_hz(np.linspace(_hz_to_mel(0.0), _hz_to_mel(rate / 2), n_mels + 2))
    diff = np.diff(filter_freqs)
    slopes = filter_freqs[None, :] - fft_freqs[:, None]
    down = -slopes[:, :-2] / diff[:-1]
    up = slopes[:, 2:] / diff[1:]
    filters = np.maximum(0.0, np.minimum(down, up))
    return filters * (2.0 / (filter_freqs[2:n_mels + 2] - filter_freqs[:n_mels]))[None, :]


_FILTERS = _mel_filters()
_WINDOW = np.hanning(401)[:-1]  # periodic Hann, 25 ms


def log_mel(audio: np.ndarray) -> np.ndarray:
    """What transformers' WhisperFeatureExtractor(chunk_length=8) gives Smart Turn, without torch or
    transformers: the last 8 s (zeros in front when shorter), normalised to zero mean and unit variance,
    80 log-mel bins every 10 ms, shape (80, 800). Parity with transformers is checked on a runner
    (voice-turn-eval.yml), not here."""
    n = WINDOW_SECONDS * SAMPLE_RATE
    x = np.asarray(audio, dtype=np.float64)
    if np.asarray(audio).dtype.kind in "iu":  # int16 PCM; float audio is taken as already in [-1, 1]
        x = x / 32768.0
    x = x[-n:] if len(x) > n else np.pad(x, (n - len(x), 0))
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    padded = np.pad(x, 200, mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(padded, 400)[::160]  # 801 frames
    power = np.abs(np.fft.rfft(frames * _WINDOW, n=400)) ** 2
    mel = np.log10(np.maximum(power @ _FILTERS, 1e-10)).T[:, :-1]
    mel = np.maximum(mel, mel.max() - 8.0)
    return ((mel + 4.0) / 4.0).astype(np.float32)


# ---------------------------------------------------------------- Smart Turn v3
SMART_TURN_REPO = "pipecat-ai/smart-turn-v3"
SMART_TURN_FILE = "smart-turn-v3.2-cpu.onnx"  # int8, ~8 MB
SMART_TURN_REVISION = "f766f81d3cfdf7737ac64aad813d91bbfd56bf93"  # pinned: what the evaluation measured


class SmartTurn:
    """Smart Turn v3 through onnxruntime on one CPU thread; the text rule first."""

    name = "smart_turn"

    def __init__(self, path: str | None = None, threshold: float = 0.5, session=None, use_rules: bool = True):
        self.session = session if session is not None else self.load_session(path)
        self.input = self.session.get_inputs()[0].name
        self.threshold, self.use_rules = threshold, use_rules

    @staticmethod
    def load_session(path: str | None):
        import onnxruntime as ort

        if not path:
            from huggingface_hub import hf_hub_download

            path = hf_hub_download(SMART_TURN_REPO, SMART_TURN_FILE, revision=SMART_TURN_REVISION)
        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        return ort.InferenceSession(path, sess_options=options, providers=["CPUExecutionProvider"])

    def probability(self, audio: np.ndarray) -> float:
        features = log_mel(audio)[None, :, :]
        return float(np.asarray(self.session.run(None, {self.input: features})[0]).reshape(-1)[0])

    def __call__(self, audio: np.ndarray, partial: str = "") -> Verdict:
        started = time.perf_counter()
        if self.use_rules and (reason := unfinished(partial)):
            return Verdict(INCOMPLETE, reason=reason, ms=(time.perf_counter() - started) * 1000)
        p = self.probability(audio)
        return Verdict(COMPLETE if p >= self.threshold else INCOMPLETE, p, "smart_turn",
                       (time.perf_counter() - started) * 1000)


ENDPOINTING = ("silence", "rules", "smart_turn")


def build_turn_detector(settings: Settings):
    """The configured detector, called as detector(audio, partial) -> Verdict; None for silence only."""
    mode = settings.voice_endpointing
    if mode not in ENDPOINTING:
        raise ValueError(f"unknown LOOPDESK_VOICE_ENDPOINTING {mode!r}")
    if mode == "silence":
        return None
    if mode == "rules":
        return RuleTurn()
    return SmartTurn(settings.voice_smart_turn_path, threshold=settings.voice_turn_threshold)
