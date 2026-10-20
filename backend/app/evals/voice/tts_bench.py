"""TTS speed on answer-like sentences: time to the first audio chunk (what the customer waits
for) and real-time factor. Sentences come from the tenants' knowledge base, the material answers
are written from. Runs on a GitHub Actions runner.

    python -m app.evals.voice.tts_bench --engine cosyvoice --url http://127.0.0.1:50000 --out tts-cosyvoice.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from ...voice.speakable import speakable, split_sentences

SEED_DIR = Path(__file__).resolve().parents[3] / "seed_data"


def sentences(n: int = 30) -> list[str]:
    picked: list[str] = []
    for tenant in ("echo", "leon", "iris"):
        for doc in sorted((SEED_DIR / tenant).glob("*.md")):
            if "internal" in doc.name:
                continue
            for line in doc.read_text(encoding="utf-8").splitlines():
                line = re.sub(r"^\s*[-*>#]+\s*", "", line).strip()
                for s in split_sentences(line):
                    if 12 <= len(s) <= 48 and "SYNTHETIC" not in s:
                        picked.append(s)
    step = max(1, len(picked) // n)
    return picked[::step][:n]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True, choices=["kokoro", "cosyvoice", "say"])
    parser.add_argument("--url", default="http://127.0.0.1:50000")
    parser.add_argument("--voice", default=None)
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    from ...voice import engines

    engine = {"kokoro": lambda: engines.KokoroTTS(args.voice), "cosyvoice": lambda: engines.CosyVoiceTTS(args.url, timeout=900),
              "say": lambda: engines.SayTTS(args.voice)}[args.engine]()
    for _ in engine.synthesize("您好，请问有什么可以帮您？"):  # warm-up, not measured
        pass
    rows = []
    for text in sentences(args.n):
        spoken = speakable(text).text
        started = time.perf_counter()
        first, samples = None, 0
        for chunk in engine.synthesize(spoken):
            if first is None:
                first = (time.perf_counter() - started) * 1000
            samples += len(chunk)
        total = time.perf_counter() - started
        audio = samples / engine.sample_rate
        rows.append({"engine": args.engine, "text": spoken, "chars": len(spoken), "first_chunk_ms": round(first or 0, 1),
                     "total_ms": round(total * 1000, 1), "audio_seconds": round(audio, 3),
                     "rtf": round(total / audio, 3) if audio else None})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
