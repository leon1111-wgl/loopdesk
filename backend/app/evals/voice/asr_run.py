"""Run one ASR engine over the synthetic test audio (on a GitHub Actions runner).

    python -m app.evals.voice.asr_run --engine paraformer --synth out/synth-kokoro out/synth-cosyvoice \\
        --noisy kokoro-zf_001-1,cosyvoice-default-1 --shard 0/6 --out out/asr-paraformer-0.jsonl

Every variant is decoded clean; the --noisy variants also with babble at SNR 20/10/5 dB. Each
engine decodes with and without the tenant's hotwords (Paraformer: SeACo's list on the final
pass; Whisper: the prompt; SenseVoice has no biasing), so the hotword gain is a paired
comparison on the same audio. The streaming pass is fed in 100 ms frames as the live channel
feeds it, and its per-chunk compute time is recorded: below 600 ms per 600 ms chunk is what
"keeps up in real time" means. Paraformer writes no punctuation: its final pass with hotwords is
also recorded punctuated (LOOPDESK_VOICE_PUNC, FunASR ct-punc), with the time that step takes.

Punctuation is text only, so earlier Paraformer transcripts can be punctuated without the audio:

    python -m app.evals.voice.asr_run --repunctuate 'prev/*/asr-paraformer-*.jsonl' --out out/asr-paraformer-punc.jsonl
"""

from __future__ import annotations

import argparse
import glob
import json
import time
from pathlib import Path

import numpy as np

from ...voice.engines import read_wav
from .audio import CONDITIONS, condition, pad, seed_of
from .dataset import read_jsonl
from .hotwords import tenant_hotwords


def _items(synth_dirs: list[str], noisy: set[str]) -> list[tuple[dict, Path, str]]:
    items = []
    for d in synth_dirs:
        root = Path(d)
        if not (root / "manifest.jsonl").exists():
            continue
        for row in read_jsonl(root / "manifest.jsonl"):
            for name in CONDITIONS:
                if name == "clean" or row["variant"] in noisy:
                    items.append((row, root / row["path"], name))
    return items


def repunctuate(patterns: list[str], out: Path, punc, model: str) -> int:
    """Earlier Paraformer records with their final pass punctuated (``final_hw_punc``, ``punc_ms``), by
    the same code the live channel runs after the final pass; ``final_hw_punc_model`` is what the model
    wrote before the between-words check."""
    from ...voice.engines import ASRResult, punctuate, warm_segmenter

    timings = ("final_hw_ms", "final_nohw_ms", "chunk_ms")  # the decoding run's, on another runner

    warm_segmenter()
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", encoding="utf-8") as fh:
        for path in sorted({f for p in patterns for f in glob.glob(p)}):
            for row in read_jsonl(Path(path)):
                if row.get("engine") != "paraformer" or row.get("final_hw") is None or row.get("repunctuated"):
                    continue
                row = {k: v for k, v in row.items() if k not in timings}
                wrote: list[str] = []
                result = punctuate(ASRResult(row["final_hw"], {"final": row["final_hw"]}),
                                   lambda text: wrote.append(punc(text)) or wrote[-1])
                row.update(final_hw_punc=result.text, final_hw_punc_model=wrote[0] if wrote else result.text,
                           punc_model=model, punc_ms=None if result.punc_ms is None else round(result.punc_ms, 2),
                           repunctuated=True)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                n += 1
    print(f"{n} transcripts punctuated -> {out}")
    return 0 if n else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["paraformer", "sensevoice", "whisper"])
    parser.add_argument("--synth", nargs="+", default=[])
    parser.add_argument("--repunctuate", nargs="+", default=[],
                        help="earlier Paraformer transcripts (jsonl globs): punctuate their final pass, decode nothing")
    parser.add_argument("--punc-model", default=None, help="FunASR punctuation model (default: engines.PUNC_CT)")
    parser.add_argument("--noisy", default="")
    parser.add_argument("--shard", default="0/1")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    from ...voice import engines

    punc_model = args.punc_model or engines.PUNC_CT
    if args.repunctuate:
        return repunctuate(args.repunctuate, Path(args.out), engines.Punctuator(args.device, model=punc_model), punc_model)
    if not args.engine or not args.synth:
        parser.error("--engine and --synth are required (or --repunctuate)")
    index, count = (int(x) for x in args.shard.split("/"))

    hotwords = tenant_hotwords()
    if args.engine == "paraformer":
        engine = engines.ParaformerASR(args.device, two_pass=True)
        try:  # LOOPDESK_VOICE_PUNC; as in the live channel, a model that will not load leaves the rest working
            punctuated = engines.PunctuatedASR(engine, engines.Punctuator(args.device, model=punc_model))
        except Exception as exc:
            print(f"punctuation model {punc_model} not loaded ({type(exc).__name__}: {exc}): no final_hw_punc", flush=True)
            punctuated = engine
    elif args.engine == "sensevoice":
        engine = engines.SenseVoiceASR(args.device)
    else:
        engine = engines.WhisperASR(device=args.device)

    items = _items(args.synth, {v for v in args.noisy.split(",") if v})
    pool = [read_wav(p)[0] for _, p, name in items[:: max(1, len(items) // 60)] if name == "clean"]
    mine = [it for it in items if seed_of(it[0]["variant"], it[0]["id"], it[2]) % count == index]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row, path, name in mine:
            clean, _ = read_wav(path)
            audio = pad(condition(clean, name, pool, f"{row['variant']}/{row['id']}"))
            words = hotwords.get(row["tenant"], [])
            record = {"id": row["id"], "tenant": row["tenant"], "key": row["key"], "question": row["question"],
                      "variant": row["variant"], "condition": name, "engine": args.engine,
                      "seconds": round(len(audio) / 16000, 3), "hotwords": len(words)}
            if args.engine == "paraformer":
                stream = punctuated.stream(words)
                for start in range(0, len(audio), 1600):  # 100 ms frames, like the browser sends
                    stream.accept(audio[start:start + 1600])
                result = stream.finish()  # streaming pass flushed + SeACo final with hotwords + punctuation
                punc_ms = result.punc_ms or 0.0
                started = time.perf_counter()
                without = engine.decode_final(audio, [])
                if punctuated is not engine:
                    record.update(final_hw_punc=result.text, punc_model=punc_model)
                record.update(streaming=result.passes["streaming"], final_hw=result.passes["final"], final_nohw=without,
                              chunk_ms=[round(c, 1) for c in result.chunk_ms],
                              final_hw_ms=round(result.final_ms - punc_ms, 1),  # as before: without punctuation
                              punc_ms=None if result.punc_ms is None else round(result.punc_ms, 2),
                              final_nohw_ms=round((time.perf_counter() - started) * 1000, 1))
            elif args.engine == "sensevoice":
                started = time.perf_counter()
                result = engine.decode(audio)
                record.update(final_nohw=result.text, final_nohw_ms=round((time.perf_counter() - started) * 1000, 1))
            else:
                started = time.perf_counter()
                with_prompt = engine.decode(audio, words)
                mid = time.perf_counter()
                plain = engine.decode(audio, [])
                record.update(final_hw=with_prompt.text, final_nohw=plain.text,
                              final_hw_ms=round((mid - started) * 1000, 1),
                              final_nohw_ms=round((time.perf_counter() - mid) * 1000, 1),
                              char_probs=with_prompt.char_probs)
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"{record['variant']} {name} {record['id']}: {record.get('final_hw') or record.get('final_nohw')}",
                  flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
