"""Synthesise the voice test set (runs on a GitHub Actions runner, not on a laptop).

    python -m app.evals.voice.synth --engine kokoro --voices zf_001,zf_002,zm_009,zm_010 \\
        --extra zf_001:0.85,zf_001:1.2 --out out/synth-kokoro
    python -m app.evals.voice.synth --engine cosyvoice --url http://127.0.0.1:50000 --out out/synth-cosyvoice
    python -m app.evals.voice.synth --engine say --voices Tingting,Meijia --out out/synth-say

Writes 16 kHz mono WAVs and manifest.jsonl (one row per utterance and voice) with the synthesis
time, so the same run also measures each engine's speed on this hardware.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np

from ...voice.engines import SAMPLE_RATE, resample, to_int16, write_wav
from .dataset import as_dict, load, write_manifest


def _engine(args):
    from ...voice import engines

    if args.engine == "kokoro":
        return engines.KokoroTTS()
    if args.engine == "cosyvoice":
        return engines.CosyVoiceTTS(args.url, timeout=900)  # on a CPU runner a sentence takes minutes
    if args.engine == "say":
        return engines.SayTTS(args.voices.split(",")[0])
    raise SystemExit(f"unknown engine {args.engine}")


def _synthesize(engine, name: str, text: str, voice: str, speed: float) -> tuple[np.ndarray, float]:
    started = time.perf_counter()
    first = None
    chunks = []
    if name == "kokoro":
        stream = engine.synthesize(text, voice=voice, speed=speed)
    elif name == "say":
        stream = engine.synthesize(text, voice=voice, rate=None if speed == 1.0 else int(175 * speed))
    else:
        engine.speaker = None if voice == "default" else voice
        stream = engine.synthesize(text)
    for chunk in stream:
        if first is None:
            first = (time.perf_counter() - started) * 1000
        chunks.append(chunk)
    audio = np.concatenate(chunks) if chunks else np.zeros(0, np.int16)
    audio = to_int16(resample(audio.astype(np.float32) / 32768, engine.sample_rate, SAMPLE_RATE))
    return audio, first or 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True, choices=["kokoro", "cosyvoice", "say"])
    parser.add_argument("--voices", default="default")
    parser.add_argument("--extra", default="", help="voice:speed pairs synthesised in addition at speed 1.0")
    parser.add_argument("--url", default="http://127.0.0.1:50000")
    parser.add_argument("--limit", type=int, default=0, help="cases per tenant (0 = all)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    out = Path(args.out)
    engine = _engine(args)
    plan = [(v, 1.0) for v in args.voices.split(",")]
    plan += [(p.split(":")[0], float(p.split(":")[1])) for p in args.extra.split(",") if p]
    rows = []
    for utterance in load(limit=args.limit):
        text = utterance.spoken_hanzi if args.engine == "kokoro" else utterance.spoken
        if not text:
            continue
        for voice, speed in plan:
            variant = f"{args.engine}-{voice}-{speed:g}"
            path = out / variant / f"{utterance.tenant}__{utterance.key}.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            started = time.perf_counter()
            audio, first_ms = _synthesize(engine, args.engine, text, voice, speed)
            took = time.perf_counter() - started
            write_wav(path, audio)
            seconds = len(audio) / SAMPLE_RATE
            rows.append({**as_dict(utterance), "engine": args.engine, "voice": voice, "speed": speed,
                         "variant": variant, "text_spoken": text, "path": str(path.relative_to(out)),
                         "seconds": round(seconds, 3), "synth_seconds": round(took, 3),
                         "first_chunk_ms": round(first_ms, 1)})
            print(f"{variant} {utterance.id} {seconds:.1f}s audio in {took:.2f}s", flush=True)
    write_manifest(out / "manifest.jsonl", rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
