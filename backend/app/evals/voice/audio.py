"""Test-audio conditions: clean, or babble noise at a given SNR, deterministic per utterance.

Babble (several other synthetic speakers talking at once) stands in for a call centre or a
street: it is speech-shaped, so it is harder for ASR than white noise at the same SNR, and a
level detector cannot tell it from the customer. The noise is synthetic, like the speech.
"""

from __future__ import annotations

import hashlib

import numpy as np

CONDITIONS = {"clean": None, "snr20": 20.0, "snr10": 10.0, "snr5": 5.0}


def seed_of(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


def babble(pool: list[np.ndarray], n: int, rng: np.random.Generator, talkers: int = 4) -> np.ndarray:
    out = np.zeros(n, dtype=np.float32)
    for _ in range(talkers):
        clip = pool[int(rng.integers(len(pool)))].astype(np.float32)
        if len(clip) == 0:
            continue
        reps = int(np.ceil((n + len(clip)) / len(clip)))
        loop = np.tile(clip, reps)
        start = int(rng.integers(len(clip)))
        out += loop[start:start + n]
    return out


def mix(clean: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    signal = clean.astype(np.float32)
    p_signal = float(np.mean(signal ** 2)) or 1e-9
    p_noise = float(np.mean(noise ** 2)) or 1e-9
    scaled = noise * np.sqrt(p_signal / (p_noise * 10 ** (snr_db / 10)))
    mixed = signal + scaled
    peak = float(np.max(np.abs(mixed))) or 1.0
    if peak > 32000:
        mixed *= 32000 / peak
    return mixed.astype(np.int16)


def condition(clean: np.ndarray, name: str, pool: list[np.ndarray], key: str) -> np.ndarray:
    snr = CONDITIONS[name]
    if snr is None:
        return clean
    rng = np.random.default_rng(seed_of(key, name))
    return mix(clean, babble(pool, len(clean), rng), snr)


def pad(audio: np.ndarray, lead: float = 0.3, tail: float = 1.2, rate: int = 16_000) -> np.ndarray:
    return np.concatenate([np.zeros(int(lead * rate), np.int16), audio, np.zeros(int(tail * rate), np.int16)])


def speech_end(audio: np.ndarray, frame: int = 160, threshold_db: float = -40.0) -> int:
    """Sample index just after the last 10 ms frame louder than threshold (on clean audio)."""
    n = len(audio) // frame
    if n == 0:
        return 0
    frames = audio[: n * frame].astype(np.float64).reshape(n, frame)
    level = 20 * np.log10(np.sqrt(np.mean(frames ** 2, axis=1)) / 32768 + 1e-12)
    loud = np.nonzero(level > threshold_db)[0]
    return int((loud[-1] + 1) * frame) if len(loud) else 0
