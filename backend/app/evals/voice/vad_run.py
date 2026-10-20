"""Endpointing: how long after the customer stops does the channel decide they are done, and how
often does it cut them off mid-sentence? Swept over trailing-silence settings for each detector,
on clean and noisy audio. Runs where the detectors are installed (Silero needs its package).

    python -m app.evals.voice.vad_run --synth out/synth-kokoro out/synth-cosyvoice --out out/vad.jsonl

The endpointer counts audio time, so these waits are what a customer gets in real time.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ...voice.endpoint import SAMPLE_RATE, EnergyVAD, Endpointer, SileroVAD
from ...voice.engines import read_wav
from .audio import condition, pad, speech_end
from .dataset import read_jsonl

SILENCES = (300, 500, 600, 800, 1000)
LEAD, TAIL = 0.5, 1.6


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--synth", nargs="+", required=True)
    parser.add_argument("--detectors", default="energy,silero")
    parser.add_argument("--conditions", default="clean,snr10")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    rows = [(Path(d), r) for d in args.synth if (Path(d) / "manifest.jsonl").exists()
            for r in read_jsonl(Path(d) / "manifest.jsonl")]
    pool = [read_wav(root / r["path"])[0] for root, r in rows[:: max(1, len(rows) // 60)]]
    factories = {"energy": EnergyVAD, "silero": SileroVAD}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.out).open("w", encoding="utf-8") as fh:
        for root, row in rows:
            clean, _ = read_wav(root / row["path"])
            true_end = int(LEAD * SAMPLE_RATE) + speech_end(clean)
            for name in args.conditions.split(","):
                audio = pad(condition(clean, name, pool, f"vad/{row['variant']}/{row['id']}"), LEAD, TAIL)
                if name != "clean":  # noise through the padding too: the room does not go quiet
                    audio = condition(audio, name, pool, f"vadpad/{row['variant']}/{row['id']}")
                for detector in args.detectors.split(","):
                    for silence in SILENCES:
                        ep = Endpointer(factories[detector](), end_silence_ms=silence)
                        ends, starts = [], 0
                        for start in range(0, len(audio), 1600):
                            for event in ep.push(audio[start:start + 1600]):
                                starts += event.kind == "start"
                                if event.kind == "end":
                                    ends.append(event)
                        first = ends[0] if ends else None
                        fh.write(json.dumps({
                            "id": row["id"], "variant": row["variant"], "condition": name, "detector": detector,
                            "end_silence_ms": silence, "started": starts > 0, "ended": first is not None,
                            # cut off: the first end came before the customer finished (a pause mid-sentence)
                            "premature": bool(first and first.at < true_end),
                            "wait_ms": round((first.at - true_end) / SAMPLE_RATE * 1000, 1) if first else None,
                            "segments": len(ends),
                        }) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
