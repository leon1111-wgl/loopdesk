"""Speech engines behind small interfaces: streaming ASR and streaming TTS.

Every model engine imports its packages when it is built, never at import time, so the server,
the tests and CI need none of them (requirements-voice.txt lists them). ``build_engines`` returns
None with a reason when the configured engines cannot be loaded, and the voice channel then
reports itself unavailable instead of failing requests.

The same classes serve the live channel and the voice evaluation (app/evals/voice), so what is
measured is the code that serves.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from ..config import Settings
from .endpoint import SAMPLE_RATE, VAD, EnergyVAD, SileroVAD

logger = logging.getLogger(__name__)


@dataclass
class ASRResult:
    text: str  # what is handed on: the final pass, punctuated when the engine writes no punctuation
    passes: dict[str, str] = field(default_factory=dict)  # each pass as decoded: {"streaming": ..., "final": ...}
    char_probs: list[tuple[str, float]] | None = None  # token, probability (engines that report it)
    final_ms: float = 0.0  # compute time of finish() after the end of the utterance
    chunk_ms: list[float] = field(default_factory=list)  # compute time of each streaming chunk
    punc_ms: float | None = None  # the punctuation step's share of final_ms (None: no punctuation step)
    unpunctuated: str | None = None  # the text before punctuation was added (None: none was added)


class ASRStream(Protocol):
    def accept(self, pcm: np.ndarray) -> str | None:
        """Feed int16 audio; returns the partial transcript when it changed."""

    def finish(self) -> ASRResult: ...


class ASREngine(Protocol):
    name: str
    punctuates: bool  # writes punctuation itself; False: LOOPDESK_VOICE_PUNC adds it to the final text

    def stream(self, hotwords: list[str]) -> ASRStream: ...


class TTSEngine(Protocol):
    name: str
    sample_rate: int

    def synthesize(self, text: str) -> Iterator[np.ndarray]:
        """int16 audio chunks; a streaming engine yields the first one before the sentence is done."""


def to_float(pcm: np.ndarray) -> np.ndarray:
    return pcm.astype(np.float32) / 32768


def to_int16(audio: np.ndarray) -> np.ndarray:
    return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)


def resample(audio: np.ndarray, rate_in: int, rate_out: int) -> np.ndarray:
    if rate_in == rate_out or len(audio) == 0:
        return audio
    try:
        from scipy.signal import resample_poly

        g = np.gcd(rate_in, rate_out)
        return resample_poly(audio.astype(np.float32), rate_out // g, rate_in // g).astype(np.float32)
    except ImportError:  # numpy only: linear interpolation, fine for speech at these rates
        n = int(round(len(audio) * rate_out / rate_in))
        return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


# ---------------------------------------------------------------- buffered (non-streaming) ASR
class _BufferedStream:
    """For engines without a streaming mode: collect the utterance, decode it once at the end."""

    def __init__(self, decode: Callable[[np.ndarray], ASRResult]):
        self.decode, self.chunks = decode, []

    def accept(self, pcm: np.ndarray) -> str | None:
        self.chunks.append(pcm)
        return None

    def finish(self) -> ASRResult:
        audio = np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.int16)
        started = time.perf_counter()
        result = self.decode(audio)
        result.final_ms = (time.perf_counter() - started) * 1000
        return result


# ---------------------------------------------------------------- FunASR Paraformer (2-pass)
PARAFORMER_STREAMING = "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
PARAFORMER_SEACO = "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
SENSEVOICE = "iic/SenseVoiceSmall"
# The punctuation model FunASR's own 2-pass server puts after the final pass; of the three FunASR
# CT-Transformers compared in docs/EVALUATION.md it was the only one that cost the agent no task. The large
# cn-en model also loads its 11 MB jieba user dictionary into the process-wide jieba retrieval uses.
PUNC_CT = "iic/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727"
CHUNK_SIZE = [0, 10, 5]  # 600 ms chunks, 300 ms look-ahead
CHUNK_SAMPLES = CHUNK_SIZE[1] * 960


def _funasr(model: str, device: str):
    from funasr import AutoModel

    return AutoModel(model=model, device=device, disable_update=True, disable_pbar=True, log_level="ERROR")


def _funasr_text(res) -> str:
    """SeACo writes "一 块 钱 hu b": characters spaced apart; the ITN removes spaces next to CJK."""
    return (res[0].get("text") or "") if res else ""


class ParaformerASR:
    """Streaming Paraformer for partial results while the customer talks, then (two_pass) SeACo-
    Paraformer over the whole utterance with the tenant's hotwords for the final text. The two
    passes are also a confidence signal: an order number they disagree on is read back."""

    name = "paraformer"
    punctuates = False

    def __init__(self, device: str = "cpu", two_pass: bool = True):
        self.device, self.two_pass = device, two_pass
        self.online = _funasr(PARAFORMER_STREAMING, device)
        self.final = _funasr(PARAFORMER_SEACO, device) if two_pass else None
        self.lock = threading.Lock()  # one decode at a time per model (CPU bound anyway)

    def stream(self, hotwords: list[str]) -> "_ParaformerStream":
        return _ParaformerStream(self, hotwords)

    def decode_final(self, audio: np.ndarray, hotwords: list[str]) -> str:
        with self.lock:
            res = self.final.generate(input=to_float(audio), **({"hotword": " ".join(hotwords)} if hotwords else {}))
        return _funasr_text(res)


class _ParaformerStream:
    def __init__(self, engine: ParaformerASR, hotwords: list[str]):
        self.engine, self.hotwords = engine, hotwords
        self.cache: dict = {}
        self.pending = np.zeros(0, dtype=np.int16)
        self.audio: list[np.ndarray] = []
        self.text = ""
        self.chunk_ms: list[float] = []

    def _chunk(self, chunk: np.ndarray, final: bool) -> None:
        started = time.perf_counter()
        with self.engine.lock:
            res = self.engine.online.generate(input=to_float(chunk), cache=self.cache, is_final=final,
                                              chunk_size=CHUNK_SIZE, encoder_chunk_look_back=4,
                                              decoder_chunk_look_back=1)
        self.chunk_ms.append((time.perf_counter() - started) * 1000)
        self.text += _funasr_text(res)

    def accept(self, pcm: np.ndarray) -> str | None:
        self.audio.append(pcm)
        self.pending = np.concatenate([self.pending, pcm])
        before = self.text
        while len(self.pending) >= CHUNK_SAMPLES:
            chunk, self.pending = self.pending[:CHUNK_SAMPLES], self.pending[CHUNK_SAMPLES:]
            self._chunk(chunk, final=False)
        return self.text if self.text != before else None

    def finish(self) -> ASRResult:
        started = time.perf_counter()
        audio = np.concatenate(self.audio) if self.audio else np.zeros(0, dtype=np.int16)
        if len(audio) < SAMPLE_RATE // 10:  # a push-to-talk tap: nothing to decode
            return ASRResult("", {"streaming": "", "final": ""}, final_ms=0.0, chunk_ms=self.chunk_ms)
        # The last call flushes the decoder; it gets a few ms of silence when the audio ended on a chunk edge.
        self._chunk(self.pending if len(self.pending) else np.zeros(160, dtype=np.int16), final=True)
        passes = {"streaming": self.text}
        text = self.text
        if self.engine.two_pass:
            text = passes["final"] = self.engine.decode_final(audio, self.hotwords)
        return ASRResult(text, passes, final_ms=(time.perf_counter() - started) * 1000, chunk_ms=self.chunk_ms)


class SenseVoiceASR:
    """SenseVoice-Small: non-streaming, fast, writes digits itself (use_itn). No hotword support."""

    name = "sensevoice"
    punctuates = True

    def __init__(self, device: str = "cpu"):
        self.model = _funasr(SENSEVOICE, device)
        self.lock = threading.Lock()

    def stream(self, hotwords: list[str]) -> _BufferedStream:
        return _BufferedStream(self.decode)

    def decode(self, audio: np.ndarray) -> ASRResult:
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        with self.lock:
            res = self.model.generate(input=to_float(audio), language="zh", use_itn=True)
        text = rich_transcription_postprocess(_funasr_text(res))
        return ASRResult(text, {"final": text})


class WhisperASR:
    """Whisper large-v3-turbo through faster-whisper (CTranslate2, int8 on CPU). Hotwords go into
    the initial prompt, the only biasing Whisper has; word probabilities feed entity confidence."""

    name = "whisper"
    punctuates = True

    def __init__(self, model: str = "large-v3-turbo", device: str = "cpu", use_prompt: bool = True):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model, device="cpu" if device == "mps" else device, compute_type="int8")
        self.use_prompt = use_prompt
        self.lock = threading.Lock()

    def stream(self, hotwords: list[str]) -> _BufferedStream:
        return _BufferedStream(lambda audio: self.decode(audio, hotwords))

    def decode(self, audio: np.ndarray, hotwords: list[str]) -> ASRResult:
        prompt = "以下是普通话客服对话。" + ("，".join(hotwords[:30]) + "。" if (hotwords and self.use_prompt) else "")
        with self.lock:
            segments, _ = self.model.transcribe(to_float(audio), language="zh", initial_prompt=prompt,
                                                word_timestamps=True, beam_size=5, vad_filter=False)
            segments = list(segments)
        text = "".join(s.text for s in segments).strip()
        probs = [(w.word, float(w.probability)) for s in segments for w in (s.words or [])]
        return ASRResult(text, {"final": text}, char_probs=probs)


# ---------------------------------------------------------------- punctuation (engines that write none)
class Punctuator:
    """FunASR's CT-Transformer punctuation model. Paraformer writes no punctuation, so "S O 二零二六…我要退货
    退款另外发票怎么开" reaches the agent as one run-on request; with the commas back it is two. Text only
    and small: it runs on the final text, never on the partials shown while the customer talks. Each
    utterance starts with an empty cache: a turn is punctuated on its own, not as the previous one's tail."""

    def __init__(self, device: str = "cpu", model: str = PUNC_CT):
        self.model_id = model
        self.model = _funasr(model, device)
        self.lock = threading.Lock()

    def __call__(self, text: str) -> str:
        with self.lock:
            res = self.model.generate(input=text, cache={})
        return _funasr_text(res) or text


_MARKS = "，。？！、；：,.?!;:"
_NUMBER = re.compile(r"[零〇一二三四五六七八九幺两十百千万亿点\d]{2,}")  # a spoken or written number: one word


def between_words(decoded: str, punctuated: str, protect: list[str] = ()) -> str:
    """The punctuation model's output, keeping only the marks it added between two words: words as jieba
    (the retrieval's segmenter, with its domain words) splits the sentence, with every number and every
    term in ``protect`` (the tenant's hotwords) kept whole. ct-punc wrote "注，销账号以后": a mark inside
    a word hides the word from retrieval. Output that changed anything but punctuation, spacing and
    letter case ("So", the large model) is not used: the decoder's words are what the customer said."""
    from ..textutil import jieba

    words = "".join(decoded.split())
    boundaries, at = set(), 0
    for word in (jieba.lcut(words) if jieba is not None else list(words)):
        at += len(word)
        boundaries.add(at)
    spans = [m.span() for m in _NUMBER.finditer(words)]
    lowered = words.lower()
    for term in filter(None, (t.lower() for t in protect)):
        start = lowered.find(term)
        while start >= 0:
            spans.append((start, start + len(term)))
            start = lowered.find(term, start + 1)
    for start, end in spans:  # one word: its two ends are word ends ("零零二|到", jieba glued "二到") ...
        boundaries.update((start, end))
    for start, end in spans:  # ... and nothing inside it is, also where two spans overlap
        boundaries.difference_update(range(start + 1, end))
    boundaries.discard(0)
    out, i = [], 0
    for ch in punctuated:  # the model's marks and spacing, the decoder's characters (and its own marks)
        if ch.isspace():
            out.append(ch)
        elif i < len(words) and ch.lower() == words[i].lower():
            out.append(words[i])
            i += 1
        elif ch in _MARKS:
            if i in boundaries:
                out.append(ch)
        else:
            break
    if i != len(words):
        logger.warning("voice punctuation changed the words, not used: %r -> %r", decoded, punctuated)
        return decoded
    return "".join(out)


def punctuate(result: ASRResult, punc: Callable[[str], str], protect: list[str] = ()) -> ASRResult:
    """Punctuate the text handed on, between words only, and time it. The text before is kept
    (``unpunctuated``): understand() falls back to it when the punctuation changed an order number, an
    amount or what PII masking finds. A model error keeps the words unpunctuated."""
    if not result.text.strip():
        return result
    started = time.perf_counter()
    try:
        text = between_words(result.text, punc(result.text), protect)
    except Exception:
        logger.warning("voice punctuation failed, the transcript stays unpunctuated", exc_info=True)
        text = None
    result.punc_ms = (time.perf_counter() - started) * 1000
    result.final_ms += result.punc_ms
    if text is not None:
        result.unpunctuated, result.text = result.text, text
    return result


class _PunctuatedStream:
    def __init__(self, stream: ASRStream, punc: Callable[[str], str], hotwords: list[str]):
        self.inner, self.punc, self.hotwords = stream, punc, hotwords

    def accept(self, pcm: np.ndarray) -> str | None:
        return self.inner.accept(pcm)

    def finish(self) -> ASRResult:
        return punctuate(self.inner.finish(), self.punc, self.hotwords)


class PunctuatedASR:
    """An engine that writes no punctuation, with ``punc`` applied to its final text (LOOPDESK_VOICE_PUNC)."""

    punctuates = True

    def __init__(self, engine: ASREngine, punc: Callable[[str], str]):
        self.engine, self.punc, self.name = engine, punc, engine.name
        warm_segmenter()  # jieba loads its dictionary on first use: not in the first customer's turn

    def stream(self, hotwords: list[str]) -> _PunctuatedStream:
        return _PunctuatedStream(self.engine.stream(hotwords), self.punc, hotwords)


def warm_segmenter() -> None:
    from ..textutil import jieba

    if jieba is not None:
        jieba.initialize()


def with_punctuation(asr: ASREngine, settings: Settings) -> ASREngine:
    """``asr`` with punctuation when it is switched on and the engine writes none. A punctuation model that
    cannot be loaded leaves the channel working, unpunctuated: the agent still gets every word."""
    if not settings.voice_punc or getattr(asr, "punctuates", False):
        return asr
    try:
        return PunctuatedASR(asr, Punctuator(settings.voice_device))
    except Exception as exc:  # missing packages or weights
        logger.warning("voice punctuation unavailable, transcripts stay unpunctuated: %s: %s", type(exc).__name__, exc)
        return asr


# ---------------------------------------------------------------- TTS
class CosyVoiceTTS:
    """CosyVoice 2 through its sidecar (backend/sidecars/cosyvoice_server.py), which runs in its own
    environment: CosyVoice pins torch and numpy versions the backend does not use. The sidecar
    streams raw int16 PCM; the first chunk arrives before the sentence is synthesised."""

    name = "cosyvoice"

    def __init__(self, url: str, speaker: str | None = None, timeout: float = 30.0):
        import httpx

        self.url, self.speaker = url.rstrip("/"), speaker
        self.client = httpx.Client(timeout=timeout)
        info = self.client.get(f"{self.url}/info").json()
        self.sample_rate = int(info["sample_rate"])

    def synthesize(self, text: str) -> Iterator[np.ndarray]:
        body = {"text": text, **({"speaker": self.speaker} if self.speaker else {})}
        with self.client.stream("POST", f"{self.url}/tts", json=body) as response:
            response.raise_for_status()
            rest = b""
            for data in response.iter_bytes():
                data = rest + data
                cut = len(data) - len(data) % 2
                rest = data[cut:]
                if cut:
                    yield np.frombuffer(data[:cut], dtype=np.int16)


class KokoroTTS:
    """Kokoro-82M v1.1-zh: small and fast on CPU, many Chinese voices. Not streaming within a
    sentence; the voice evaluation uses its voices for speaker variety."""

    name = "kokoro"
    sample_rate = 24_000
    REPO = "hexgrad/Kokoro-82M-v1.1-zh"

    def __init__(self, voice: str | None = None, speed: float = 1.0):
        from kokoro import KModel, KPipeline

        self.model = KModel(repo_id=self.REPO).eval()
        self.pipeline = KPipeline(lang_code="z", repo_id=self.REPO, model=self.model)
        self.voice, self.speed = voice or "zf_001", speed

    def synthesize(self, text: str, voice: str | None = None, speed: float | None = None) -> Iterator[np.ndarray]:
        for result in self.pipeline(text, voice=voice or self.voice, speed=speed or self.speed):
            audio = result.audio.numpy() if hasattr(result.audio, "numpy") else np.asarray(result.audio)
            yield to_int16(audio)


class SayTTS:
    """macOS `say`: no model to download, the baseline on a Mac. One chunk per sentence."""

    name = "say"
    sample_rate = SAMPLE_RATE

    def __init__(self, voice: str | None = None, rate: int | None = None):
        self.voice, self.rate = voice or "Tingting", rate
        if subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True).stdout.find(self.voice) < 0:
            raise RuntimeError(f"macOS voice {self.voice} is not installed")

    def synthesize(self, text: str, voice: str | None = None, rate: int | None = None) -> Iterator[np.ndarray]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.wav"
            cmd = ["say", "-v", voice or self.voice, "--data-format=LEI16@16000", "-o", str(path)]
            if rate or self.rate:
                cmd += ["-r", str(rate or self.rate)]
            subprocess.run(cmd + [text], check=True, timeout=60)
            yield read_wav(path)[0]


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2:
            raise ValueError("16-bit PCM WAV expected")
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            audio = audio.reshape(-1, w.getnchannels())[:, 0].copy()
        return audio, w.getframerate()


def write_wav(path: str | Path, audio: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.astype(np.int16).tobytes())


# ---------------------------------------------------------------- fakes for tests
class ScriptedASR:
    """Returns the next scripted transcript for each utterance. ``passes`` lets a test give the
    streaming pass a different reading than the final one."""

    name = "scripted"
    punctuates = True  # a scripted transcript is used as written

    def __init__(self, transcripts: list[str | dict] | None = None, cycle: bool = False):
        self.transcripts, self.cycle = list(transcripts or []), cycle

    def stream(self, hotwords: list[str]) -> _BufferedStream:
        def decode(audio: np.ndarray) -> ASRResult:
            item = self.transcripts.pop(0) if self.transcripts else ""
            if self.cycle and item:
                self.transcripts.append(item)
            if isinstance(item, dict):
                return ASRResult(item["final"], {k: v for k, v in item.items()})
            return ASRResult(item, {"final": item})

        return _BufferedStream(decode)


class ToneTTS:
    """A quiet tone, 60 ms of audio per character, in 100 ms chunks. Deterministic and instant."""

    name = "tone"
    sample_rate = SAMPLE_RATE

    def __init__(self, ms_per_char: int = 60):
        self.ms_per_char = ms_per_char

    def synthesize(self, text: str) -> Iterator[np.ndarray]:
        n = max(1, len(text)) * self.ms_per_char * SAMPLE_RATE // 1000
        audio = (np.sin(np.arange(n) * 2 * np.pi * 220 / SAMPLE_RATE) * 1000).astype(np.int16)
        for start in range(0, n, SAMPLE_RATE // 10):
            yield audio[start:start + SAMPLE_RATE // 10]


@dataclass
class VoiceEngines:
    asr: ASREngine
    tts: TTSEngine
    vad: Callable[[], VAD] = EnergyVAD  # a fresh detector per session: model VADs keep state

    def describe(self) -> dict:
        if isinstance(self.asr, PunctuatedASR):  # which punctuation model is live, if any
            punctuation = getattr(self.asr.punc, "model_id", "custom")
        else:
            punctuation = "engine" if getattr(self.asr, "punctuates", False) else None
        return {"asr": self.asr.name, "tts": self.tts.name, "tts_sample_rate": self.tts.sample_rate,
                "punctuation": punctuation}


def build_engines(settings: Settings, asr: ASREngine | None = None
                  ) -> tuple[VoiceEngines | None, str | None, ASREngine | None]:
    """The configured engines, or None and why. Also returns the ASR when it loaded, so a retry after
    a TTS failure (a sidecar that was down) does not load the ASR models again."""
    if settings.voice_asr == "off" or settings.voice_tts == "off":
        return None, "语音通道未启用（LOOPDESK_VOICE_ASR / LOOPDESK_VOICE_TTS）", None
    if (settings.voice_asr == "scripted" or settings.voice_tts == "tone") and not settings.seed_demo:
        return None, "测试用语音引擎只能在演示模式下使用", None
    try:
        if asr is None:
            if settings.voice_asr == "scripted":
                asr = ScriptedASR([t for t in settings.voice_scripted_text.split("|") if t], cycle=True)
            elif settings.voice_asr == "paraformer":
                asr = with_punctuation(ParaformerASR(settings.voice_device), settings)
            elif settings.voice_asr == "sensevoice":
                asr = SenseVoiceASR(settings.voice_device)
            elif settings.voice_asr == "whisper":
                asr = WhisperASR(device=settings.voice_device)
            else:
                raise ValueError(f"unknown LOOPDESK_VOICE_ASR {settings.voice_asr!r}")
    except Exception as exc:  # missing packages or weights
        logger.warning("voice channel unavailable: %s: %s", type(exc).__name__, exc)
        return None, f"语音识别引擎加载失败：{type(exc).__name__}", None
    try:
        tts: TTSEngine
        if settings.voice_tts == "tone":
            tts = ToneTTS()
        elif settings.voice_tts == "cosyvoice":
            tts = CosyVoiceTTS(settings.voice_cosyvoice_url, settings.voice_tts_voice)
        elif settings.voice_tts == "kokoro":
            tts = KokoroTTS(settings.voice_tts_voice)
        elif settings.voice_tts == "say":
            tts = SayTTS(settings.voice_tts_voice)
        else:
            raise ValueError(f"unknown LOOPDESK_VOICE_TTS {settings.voice_tts!r}")
        vad: Callable[[], VAD] = SileroVAD if settings.voice_vad == "silero" else EnergyVAD
        if settings.voice_vad == "silero":
            SileroVAD()  # fail now, not in the first session
        return VoiceEngines(asr, tts, vad), None, asr
    except Exception as exc:  # a sidecar that is down, missing packages
        logger.warning("voice channel unavailable: %s: %s", type(exc).__name__, exc)
        return None, f"语音合成引擎加载失败：{type(exc).__name__}", asr


_engines: VoiceEngines | None = None
_problem: str | None = None
_loaded_asr: ASREngine | None = None
_failed_at = 0.0
_lock = threading.Lock()
_clock = time.monotonic
RETRY_SECONDS = 60.0  # a sidecar that was down at the first connection is tried again after this


def get_engines(settings: Settings) -> tuple[VoiceEngines | None, str | None]:
    """Built once per process, on first use (loading models takes seconds); after a failure, again at
    most once a minute, so a sidecar that comes up later is picked up without a restart."""
    global _engines, _problem, _failed_at, _loaded_asr
    with _lock:
        if _engines is None and (_problem is None or _clock() - _failed_at >= RETRY_SECONDS):
            _engines, _problem, _loaded_asr = build_engines(settings, _loaded_asr)
            _failed_at = _clock()
        return _engines, _problem


def set_engines(engines: VoiceEngines | None, problem: str | None = None) -> None:
    """Tests install fake engines here."""
    global _engines, _problem, _loaded_asr
    with _lock:
        _engines, _problem, _loaded_asr = engines, problem, None


def describe(engines: VoiceEngines | None) -> str:
    return json.dumps(engines.describe() if engines else {}, ensure_ascii=False)
