"""Cantonese and Mandarin-English code-switching: decode public recordings with each engine and setting
(runs on a runner; voice-yue-eval.yml). Measure only: nothing here is tuned on these sets.

    python -m app.evals.voice.yue_run --engine sensevoice --synth out/synth-fleurs-yue out/synth-ascend \\
        --shard 0/4 --out out/yue/yue-sensevoice-0.jsonl

Sets: FLEURS yue_hant_hk test (CC BY 4.0, Cantonese read in Hong Kong, Traditional references) and the
"mixed" utterances of ASCEND's test split (CC BY-SA 4.0, Mandarin-English conversation recorded in Hong
Kong). Engines and settings, each decoding the whole utterance (as the final pass does):
- paraformer: SeACo-Paraformer, the channel's Mandarin final pass (no Cantonese training);
- sensevoice: SenseVoice-Small with language yue / auto / zh;
- whisper: large-v3-turbo (faster-whisper int8) with language yue / zh / detected;
- funasr-nano: Fun-ASR-Nano-2512 (0.8B, LLM decoder), if it loads and keeps up on a CPU runner.
Rows: the reference, the hypothesis, the compute time. yue_report.py scores them.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


from ...voice.engines import read_wav, to_float
from .audio import seed_of
from .dataset import read_jsonl

SETTINGS = {
    "paraformer": {"fleurs-yue-test": [None], "ascend-test": [None]},
    "sensevoice": {"fleurs-yue-test": ["yue", "auto"], "ascend-test": ["auto", "zh"]},
    "whisper": {"fleurs-yue-test": ["yue", "zh"], "ascend-test": [None, "zh"]},
    "funasr-nano": {"fleurs-yue-test": [None], "ascend-test": [None]},
}


def load(engine: str, device: str, nano_code: str | None):
    """A decode(audio, language) -> text function for the engine."""
    from ...voice import engines

    if engine == "paraformer":
        model = engines._funasr(engines.PARAFORMER_SEACO, device)
        return lambda audio, language: engines._funasr_text(model.generate(input=to_float(audio)))
    if engine == "sensevoice":
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        model = engines._funasr(engines.SENSEVOICE, device)
        return lambda audio, language: rich_transcription_postprocess(
            engines._funasr_text(model.generate(input=to_float(audio), language=language, use_itn=True)))
    if engine == "whisper":
        from faster_whisper import WhisperModel

        model = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")

        def whisper(audio, language):
            segments, _ = model.transcribe(to_float(audio), language=language, beam_size=5, vad_filter=False)
            return "".join(s.text for s in segments).strip()
        return whisper
    if engine == "funasr-nano":
        from funasr import AutoModel

        model = AutoModel(model="FunAudioLLM/Fun-ASR-Nano-2512", trust_remote_code=True, remote_code=nano_code,
                          device=device, hub="hf", disable_update=True)

        def nano(audio, language):
            res = model.generate(input=[to_float(audio)], cache={}, batch_size=1, itn=True)
            return (res[0].get("text") if res else "") or ""
        return nano
    raise ValueError(engine)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True, choices=sorted(SETTINGS))
    parser.add_argument("--synth", nargs="+", required=True)
    parser.add_argument("--shard", default="0/1")
    parser.add_argument("--limit", type=int, default=0, help="utterances per set at most (slow engines)")
    parser.add_argument("--nano-code", default=None, help="Fun-ASR's model.py (github.com/FunAudioLLM/Fun-ASR)")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    index, count = (int(x) for x in args.shard.split("/"))
    rows = []
    for d in args.synth:
        if (Path(d) / "manifest.jsonl").exists():
            items = sorted(read_jsonl(Path(d) / "manifest.jsonl"), key=lambda r: seed_of("yue", r["id"]))
            rows += [(Path(d), r) for r in (items[: args.limit] if args.limit else items)]
    mine = [(root, r) for root, r in rows if seed_of(r["id"]) % count == index]
    decode = load(args.engine, args.device, args.nano_code)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for root, row in mine:
            audio, _ = read_wav(root / row["path"])
            for language in SETTINGS[args.engine].get(row["variant"], [None]):
                started = time.perf_counter()
                try:
                    hyp, error = decode(audio, language), None
                except Exception as exc:  # one bad file must not lose the shard
                    hyp, error = "", f"{type(exc).__name__}: {exc}"[:300]
                ms = (time.perf_counter() - started) * 1000
                config = args.engine + (f"-{language}" if language else ("" if args.engine in ("paraformer", "funasr-nano")
                                                                              else "-detect"))
                fh.write(json.dumps({"id": row["id"], "variant": row["variant"], "lang": row.get("lang"),
                                     "question": row["question"], "engine": args.engine, "config": config,
                                     "hyp": hyp, "ms": round(ms, 1), "seconds": row["seconds"],
                                     **({"error": error} if error else {})}, ensure_ascii=False) + "\n")
                fh.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
