"""Is voice/turn.py's numpy log-mel what Smart Turn was trained on? Compared on a runner with the
reference pipeline (pipecat-ai/smart-turn inference.py: transformers' WhisperFeatureExtractor), on real
recordings, feature by feature and probability by probability.

    python -m app.evals.voice.turn_parity --synth out/synth-* --n 60 --out out/parity.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from ...voice.engines import read_wav
from ...voice.turn import SmartTurn, log_mel
from .audio import seed_of
from .dataset import read_jsonl


def reference_features(audio: np.ndarray) -> np.ndarray:
    """inference.py, verbatim in substance: float audio, last 8 s or zeros in front, then the extractor."""
    from transformers import WhisperFeatureExtractor

    x = audio.astype(np.float32) / 32768.0
    n = 8 * 16000
    x = x[-n:] if len(x) > n else np.pad(x, (n - len(x), 0))
    extractor = WhisperFeatureExtractor(chunk_length=8)
    inputs = extractor(x, sampling_rate=16000, return_tensors="np", padding="max_length", max_length=n,
                       truncation=True, do_normalize=True)
    return inputs.input_features.squeeze(0).astype(np.float32)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--synth", nargs="+", required=True)
    parser.add_argument("--n", type=int, default=60)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    rows = [(Path(d), r) for d in args.synth if (Path(d) / "manifest.jsonl").exists()
            for r in read_jsonl(Path(d) / "manifest.jsonl")]
    rows = sorted(rows, key=lambda x: seed_of("parity", x[1]["id"]))[: args.n]
    smart = SmartTurn(use_rules=False)
    feature_diff, prob_diff, decisions_differ = [], [], 0
    for root, row in rows:
        audio, _ = read_wav(root / row["path"])
        for cut in (len(audio), len(audio) * 2 // 3):  # the whole sentence, and mid-sentence
            ours, ref = log_mel(audio[:cut]), reference_features(audio[:cut])
            feature_diff.append(float(np.abs(ours - ref).max()))
            p_ours = float(smart.session.run(None, {smart.input: ours[None]})[0].reshape(-1)[0])
            p_ref = float(smart.session.run(None, {smart.input: ref[None]})[0].reshape(-1)[0])
            prob_diff.append(abs(p_ours - p_ref))
            decisions_differ += (p_ours >= 0.5) != (p_ref >= 0.5)
    result = {"windows": len(feature_diff), "feature_max_abs_diff": max(feature_diff),
              "feature_median_max_abs_diff": float(np.median(feature_diff)), "probability_max_abs_diff": max(prob_diff),
              "probability_median_abs_diff": float(np.median(prob_diff)), "decisions_differ_at_0.5": decisions_differ}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
