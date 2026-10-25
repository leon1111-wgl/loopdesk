"""Semantic endpointing on real and synthetic pauses (runs on a runner; voice-turn-eval.yml).

    python -m app.evals.voice.turn_run --synth out/synth-* --shard 0/8 --out out/turn/turn-0.jsonl

Every recording is one sentence. It is padded (0.5 s before, 3.5 s after; with babble, the noise goes
through the padding too) and, once per recording and condition:
- Silero VAD's probability for every 32 ms frame;
- the streaming Paraformer partial transcript after every 100 ms push (what the text rule sees in
  production: the endpointer looks at a push before the ASR has been fed it, so the partial lags a push).
Then every configuration in ``configs()`` replays the recording through the production Endpointer
(voice/endpoint.py): the Silero VAD replays the stored probabilities (the energy VAD runs as is), and
Smart Turn is computed once per distinct audio window and remembered across configurations.

Rows (one per recording, condition and configuration): the true end of speech, every end decision
(sample position, reason, compute time of the call that decided it) and every speech start after it
(where a continuation would take the answer back). A second file has each Smart Turn call (position,
P(complete), ms) for the classifier view. turn_report.py turns both into numbers and curves; it is
pure Python and needs neither the models nor the audio.
"""

from __future__ import annotations

import argparse
import bisect
import json
import time
from pathlib import Path

import numpy as np

from ...textutil import ORDER_ID_RE
from ...voice.endpoint import FRAME, SAMPLE_RATE, EnergyVAD, Endpointer
from ...voice.engines import read_wav
from ...voice.turn import COMPLETE, INCOMPLETE, UNSURE, Verdict, unfinished
from .audio import condition, pad, speech_end
from .dataset import read_jsonl

LEAD, TAIL = 0.5, 3.5  # the tail outlasts the longest wait swept (2.5 s) plus the probe
PUSH = 1600  # the browser sends 100 ms of audio per message


def configs() -> list[dict]:
    """The sweep. Silence only (today: energy 600 ms), the text rule on top of a silence threshold, Smart
    Turn with the rule, and Smart Turn alone (to see what the rule adds)."""
    out = []
    for vad in ("energy", "silero"):
        for silence in (300, 500, 600, 800, 1000, 1200, 1500):
            out.append({"family": f"silence-{vad}", "vad": vad, "mode": "silence", "end_silence_ms": silence})
        for silence in (500, 600, 800):
            for cap in (1500, 2000, 2500):
                out.append({"family": "rules", "vad": vad, "mode": "rules", "end_silence_ms": silence,
                            "probe_ms": min(250, silence), "max_wait_ms": cap})
        for probe in (200, 250, 300):
            for cap in (1500, 2000, 2500):
                for threshold in (0.3, 0.5, 0.7, 0.9):
                    out.append({"family": "smart_turn", "vad": vad, "mode": "smart_turn", "probe_ms": probe,
                                "max_wait_ms": cap, "threshold": threshold})
        for probe in (200, 250, 300):
            out.append({"family": "smart_turn_only", "vad": vad, "mode": "smart_turn_only", "probe_ms": probe,
                        "max_wait_ms": 2000, "threshold": 0.5})
    for c in out:
        c.setdefault("end_silence_ms", 600)
        c["name"] = name_of(c)
    return out


def name_of(c: dict) -> str:
    if c["mode"] == "silence":
        return f"silence-{c['vad']}-{c['end_silence_ms']}"
    if c["mode"] == "rules":
        return f"rules-{c['vad']}-s{c['end_silence_ms']}-c{c['max_wait_ms']}"
    return f"{c['mode']}-{c['vad']}-p{c['probe_ms']}-c{c['max_wait_ms']}-t{c['threshold']}"


class ReplayVAD:
    """Stored per-frame probabilities, in order (the Endpointer asks for every frame)."""

    def __init__(self, probs: list[float]):
        self.probs, self.i = probs, 0

    def __call__(self, frame: np.ndarray) -> float:
        p = self.probs[self.i] if self.i < len(self.probs) else 0.0
        self.i += 1
        return p


class PartialTrack:
    """The streaming transcript over time: (sample position after a push, text so far), in order."""

    def __init__(self, points: list[tuple[int, str]]):
        self.points = sorted(points)
        self.positions = [p for p, _ in self.points]

    def at(self, position: int) -> str:
        """What the rule sees when the endpointer decides at ``position``: the ASR is a push behind."""
        i = bisect.bisect_right(self.positions, position - PUSH) - 1
        return self.points[i][1] if i >= 0 else ""


def replay(audio: np.ndarray, probs: list[float], partial: PartialTrack, cfg: dict, probability, memo: dict) -> dict:
    """One configuration over one recording. ``probability(window, position)`` is Smart Turn's P(complete);
    ``memo`` keeps (position, window length) -> (P, ms) across configurations."""
    vad = ReplayVAD(probs) if cfg["vad"] == "silero" else EnergyVAD()
    mode = cfg["mode"]
    holder: dict = {}

    def detector(window: np.ndarray) -> Verdict:
        position = holder["ep"].position
        if mode in ("rules", "smart_turn") and (reason := unfinished(partial.at(position))):
            return Verdict(INCOMPLETE, reason=reason)
        if mode == "rules":
            return Verdict(UNSURE, reason="rule")
        key = (position, len(window))
        if key not in memo:
            started = time.perf_counter()
            p = float(probability(window, position))
            memo[key] = (p, (time.perf_counter() - started) * 1000)
        p, ms = memo[key]
        return Verdict(COMPLETE if p >= cfg["threshold"] else INCOMPLETE, p, "smart_turn", ms)

    ep = Endpointer(vad, end_silence_ms=cfg["end_silence_ms"], turn=None if mode == "silence" else detector,
                    probe_ms=cfg.get("probe_ms", 250), max_wait_ms=cfg.get("max_wait_ms", 2000))
    holder["ep"] = ep
    ends, starts = [], []
    for start in range(0, len(audio), PUSH):
        for event in ep.push(audio[start:start + PUSH]):
            if event.kind == "start":
                starts.append(event.at + len(event.audio))  # when it was detected, not where the preroll begins
            elif event.kind == "end":
                ends.append([event.at, event.reason, round((event.verdict or {}).get("ms", 0.0), 2)])
    return {"ends": ends, "starts": starts, "started": bool(starts)}


def group_of(variant: str) -> str:
    return variant if variant.startswith(("aishell1-", "fleurs-")) else "synthetic"


def _shard(rows: list, spec: str) -> list:
    index, total = (int(x) for x in spec.split("/"))
    return [r for i, r in enumerate(rows) if i % total == index]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--synth", nargs="+", required=True, help="manifest folders (synthetic and public sets)")
    parser.add_argument("--babble", default="synthetic,aishell1-test",
                        help="groups that also get babble at SNR 10 dB (the pool: the same group's own recordings)")
    parser.add_argument("--shard", default="0/1")
    parser.add_argument("--smart-turn", default=None, help="a local Smart Turn ONNX file (default: the pinned download)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    from ...voice.endpoint import SileroVAD
    from ...voice.engines import ParaformerASR
    from ...voice.turn import SmartTurn

    rows = [(Path(d), r) for d in args.synth if (Path(d) / "manifest.jsonl").exists()
            for r in read_jsonl(Path(d) / "manifest.jsonl")]
    pools: dict[str, list[np.ndarray]] = {}
    for group in set(args.babble.split(",")):
        members = [(root, r) for root, r in rows if group_of(r["variant"]) == group]
        pools[group] = [read_wav(root / r["path"])[0] for root, r in members[:: max(1, len(members) // 60)]]
    smart = SmartTurn(args.smart_turn, use_rules=False)
    asr = ParaformerASR(two_pass=False)
    silero = SileroVAD()
    sweep = configs()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh, out.with_name(out.stem + "-asks.jsonl").open("w", encoding="utf-8") as asks_fh:
        for root, row in _shard(rows, args.shard):
            clean, _ = read_wav(root / row["path"])
            group = group_of(row["variant"])
            true_end = int(LEAD * SAMPLE_RATE) + speech_end(clean)
            order_id = bool(ORDER_ID_RE.search(row["question"] or ""))  # the customer reads an order number out
            for name in ["clean"] + (["snr10"] if group in pools and pools[group] else []):
                audio = pad(condition(clean, name, pools.get(group, []), f"turn/{row['variant']}/{row['id']}"), LEAD, TAIL)
                if name != "clean":
                    audio = condition(audio, name, pools[group], f"turnpad/{row['variant']}/{row['id']}")
                silero._model.reset_states()  # a fresh session per recording, as in production
                probs = [silero(audio[i:i + FRAME]) for i in range(0, len(audio) - FRAME + 1, FRAME)]
                stream, points = asr.stream([]), []
                for start in range(0, len(audio), PUSH):
                    if (text := stream.accept(audio[start:start + PUSH])) is not None:
                        points.append((start + PUSH, text))
                partial = PartialTrack(points)
                memo: dict = {}
                for cfg in sweep:
                    result = replay(audio, probs, partial, cfg, lambda window, position: smart.probability(window), memo)
                    fh.write(json.dumps({"id": row["id"], "variant": row["variant"], "group": group, "condition": name,
                                         "order_id": order_id, "config": cfg["name"], "family": cfg["family"],
                                         "true_end": true_end, **result}, ensure_ascii=False) + "\n")
                asks_fh.write(json.dumps({"id": row["id"], "group": group, "condition": name, "true_end": true_end,
                                          "asks": [[pos, round(p, 4), round(ms, 2)] for (pos, _), (p, ms) in sorted(memo.items())],
                                          "final_partial": points[-1][1] if points else ""}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
