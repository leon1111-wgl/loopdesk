"""Report for the real-speech check (public corpora, app/evals/voice/public_set.py). Pure Python.

    python -m app.evals.voice.public_report --asr 'out/asr-*/*.jsonl' --vad 'out/vad/*.jsonl' --out out/report

Kept apart from the synthetic report on purpose: these sentences are news and encyclopaedia text read
aloud, not customer-service questions, so there are no order numbers, plan names or task outcomes
here. Per corpus and engine:
- CER on canonical text (as for the synthetic set) and the sentence error rate;
- numbers: every number in the reference (after canonicalisation: years, amounts, counts, percentages)
  found in the hypothesis, the closest these corpora come to the entities the agent depends on;
- amounts the way the agent would receive them (ITN on the engine's text) versus the raw text;
- how often a real speaker's pause cut the utterance off, per trailing-silence setting;
- the engine's compute time after the end of an utterance.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import re
from pathlib import Path

from ...voice.itn import canonical, extract_entities, itn
from .dataset import read_jsonl
from .report import CONFIGS, edits, pct

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def numbers(text: str) -> list[str]:
    return _NUMBER.findall(canonical(text))


def amounts(text: str, use_itn: bool = True) -> set[str]:
    source = itn(text).text if use_itn else " ".join(text.split())
    return {e.value for e in extract_entities(source) if e.kind == "amount"}


def tables(records: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], dict] = collections.defaultdict(lambda: collections.Counter())
    for r in records:
        reference = r["question"]
        ref_c, ref_numbers, ref_amounts = canonical(reference), numbers(reference), amounts(reference)
        for config, (engine, field) in CONFIGS.items():
            if r["engine"] != engine or r.get(field) is None:
                continue
            hyp = r[field]
            g = groups[(r["variant"], config)]
            g["n"] += 1
            g["edits"] += edits(ref_c, canonical(hyp))
            g["chars"] += len(ref_c)
            g["sentence_errors"] += canonical(hyp) != ref_c
            hyp_numbers = collections.Counter(numbers(hyp))
            for value, count in collections.Counter(ref_numbers).items():
                g["numbers"] += count
                g["numbers_hit"] += min(count, hyp_numbers.get(value, 0))
            if ref_amounts:
                g["amounts"] += len(ref_amounts)
                g["amounts_hit_itn"] += len(ref_amounts & amounts(hyp))
                g["amounts_hit_raw"] += len(ref_amounts & amounts(hyp, use_itn=False))
    rows = []
    for (variant, config), g in sorted(groups.items()):
        rows.append({
            "corpus": variant, "config": config, "n": g["n"],
            "cer": round(g["edits"] / g["chars"], 4) if g["chars"] else None,
            "sentence_error_rate": round(g["sentence_errors"] / g["n"], 4) if g["n"] else None,
            "numbers": g["numbers"], "number_accuracy": round(g["numbers_hit"] / g["numbers"], 4) if g["numbers"] else None,
            "amounts": g["amounts"],
            "amount_accuracy_itn": round(g["amounts_hit_itn"] / g["amounts"], 4) if g["amounts"] else None,
            "amount_accuracy_raw": round(g["amounts_hit_raw"] / g["amounts"], 4) if g["amounts"] else None,
        })
    return rows


def endpointing(vad: list[dict]) -> list[dict]:
    by = collections.defaultdict(list)
    for row in vad:
        by[(row["variant"], row["detector"], row["end_silence_ms"])].append(row)
    out = []
    for (variant, detector, silence), rows in sorted(by.items()):
        waits = [r["wait_ms"] for r in rows if r["wait_ms"] is not None and not r["premature"]]
        out.append({"corpus": variant, "detector": detector, "end_silence_ms": silence, "n": len(rows),
                    "cut_off_rate": round(sum(r["premature"] for r in rows) / len(rows), 4),
                    "wait_ms_p50": pct(waits, 0.5), "missed_rate": round(sum(not r["ended"] for r in rows) / len(rows), 4)})
    return out


def latency(records: list[dict]) -> dict:
    out = {}
    for engine, field in (("paraformer", "final_hw_ms"), ("sensevoice", "final_nohw_ms"), ("whisper", "final_nohw_ms")):
        values = [r[field] for r in records if r["engine"] == engine and r.get(field) is not None]
        rtf = [r[field] / 1000 / r["seconds"] for r in records if r["engine"] == engine and r.get(field) and r["seconds"]]
        if values:
            out[engine] = {"n": len(values), "final_ms_p50": pct(values, 0.5), "final_ms_p95": pct(values, 0.95),
                           "rtf_p50": round(sorted(rtf)[len(rtf) // 2], 3) if rtf else None}
    return out


def markdown(report: dict) -> str:
    lines = ["# Real-speech check (public corpora, GitHub Actions CPU runner)", "",
             "| corpus | config | n | CER | sentence errors | numbers | amounts (ITN) | amounts (raw) |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    fmt = lambda v: "–" if v is None else f"{v:.1%}"  # noqa: E731
    for r in report["asr"]:
        lines.append(f"| {r['corpus']} | {r['config']} | {r['n']} | {fmt(r['cer'])} | {fmt(r['sentence_error_rate'])} | "
                     f"{fmt(r['number_accuracy'])} ({r['numbers']}) | {fmt(r['amount_accuracy_itn'])} ({r['amounts']}) | "
                     f"{fmt(r['amount_accuracy_raw'])} |")
    lines += ["", "| corpus | detector | end silence | cut off | wait p50 | missed |", "|---|---|---:|---:|---:|---:|"]
    for r in report["endpointing"]:
        lines.append(f"| {r['corpus']} | {r['detector']} | {r['end_silence_ms']} | {r['cut_off_rate']:.1%} | {r['wait_ms_p50']} | "
                     f"{r['missed_rate']:.1%} |")
    lines += ["", "```json", json.dumps(report["latency"], indent=1), "```"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr", nargs="+", required=True)
    parser.add_argument("--vad", nargs="*", default=[])
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    rows = lambda patterns: [r for p in patterns for f in sorted(glob.glob(p)) for r in read_jsonl(Path(f))]  # noqa: E731
    records = rows(args.asr)
    if not records:
        print("no ASR records: did the ASR jobs fail?")
        return 1
    report = {"asr": tables(records), "endpointing": endpointing(rows(args.vad)), "latency": latency(records)}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "report.md").write_text(markdown(report), encoding="utf-8")
    print(markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
