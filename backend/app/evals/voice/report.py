"""Voice evaluation report from the runner's outputs. Pure Python: runs anywhere.

    python -m app.evals.voice.report --asr out/asr-*.jsonl --vad out/vad.jsonl \\
        --tts out/tts-*.jsonl --synth out/synth-* --out out/report

Metrics, each against the written question (what the agent should receive):
- CER on canonical text (both sides: NFKC, no punctuation or spaces, numerals as digits), so a
  number written "7天" or "七天" is not an error; this is recognition quality.
- Entity accuracy on what the agent would actually receive, with ITN and hotword correction on
  and off: order numbers, amounts, plan and product names. This is where ITN and hotwords pay.
- Exact match: the whole question came through with no error that matters (canonical equality).
- Read-back flags: how often a wrong order number would have been read back (recall), and how
  often a right one would have been (the cost: an extra question).
"""

from __future__ import annotations

import argparse
import difflib
import glob
import json
import statistics
from collections import defaultdict
from pathlib import Path

from ...voice.engines import ASRResult
from ...voice.itn import assess, canonical, extract_entities
from ...voice.lexicon import Lexicon
from ...voice.session import understand
from .dataset import read_jsonl
from .hotwords import tenant_hotwords

CONFIGS = {  # name: (engine, hypothesis field)
    "paraformer-streaming": ("paraformer", "streaming"),
    "paraformer-2pass": ("paraformer", "final_nohw"),
    "paraformer-2pass+hotwords": ("paraformer", "final_hw"),
    "paraformer-2pass+hotwords+punc": ("paraformer", "final_hw_punc"),  # LOOPDESK_VOICE_PUNC: ct-punc on the final pass
    "sensevoice": ("sensevoice", "final_nohw"),
    "whisper": ("whisper", "final_nohw"),
    "whisper+prompt": ("whisper", "final_hw"),
}
TEXT_MODES = ("raw", "itn", "itn+lexicon")


def edits(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))], 1)


def agent_text(hyp: str, mode: str, lexicon: Lexicon) -> str:
    result = ASRResult(hyp, {"final": hyp})
    if mode == "raw":
        return understand(result, Lexicon([]), use_itn=False, correct=False).text
    return understand(result, lexicon, correct=mode == "itn+lexicon").text


def respelled(record: dict) -> bool:
    """Kokoro's script spells Latin words in Chinese characters (Echo -> 艾扣): what it says is not the
    word, so no engine can be expected to write the word back, and its errors must not become aliases."""
    return record["variant"].startswith("kokoro")


def _latin(term: str) -> bool:
    return any(c.isascii() and c.isalpha() for c in term)


def entity_hits(reference: str, hypothesis: str, terms: list[str]) -> dict[str, tuple[int, int]]:
    want = {(e.kind, e.value) for e in extract_entities(reference, terms)}
    got = {(e.kind, e.value) for e in extract_entities(hypothesis, terms)}
    out: dict[str, tuple[int, int]] = {}
    for kind in ("order_id", "amount", "term"):
        mine = {w for w in want if w[0] == kind}
        if mine:
            out[kind] = (len(mine & got), len(mine))
    return out


def asr_tables(records: list[dict], hotwords: dict[str, list[str]]) -> dict:
    lexicons = {t: Lexicon(words) for t, words in hotwords.items()}
    groups: dict[tuple, dict] = defaultdict(lambda: {"edits": 0, "chars": 0, "n": 0, "exact": 0,
                                                     "entities": defaultdict(lambda: [0, 0])})
    readback = defaultdict(lambda: {"wrong": 0, "wrong_flagged": 0, "right": 0, "right_flagged": 0})
    aliases: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in records:
        terms = hotwords.get(r["tenant"], [])
        if respelled(r):
            terms = [t for t in terms if not _latin(t)]
        for config, (engine, field) in CONFIGS.items():
            if r["engine"] != engine or r.get(field) is None:
                continue
            hyp = r[field]
            ref_c, hyp_c = canonical(r["question"]), canonical(hyp)
            for mode in TEXT_MODES:
                text = agent_text(hyp, mode, lexicons.get(r["tenant"], Lexicon([])))
                for scope in ((config, r["condition"], "all", mode), (config, r["condition"], r["variant"], mode)):
                    g = groups[scope]
                    if mode == "itn+lexicon":
                        g["edits"] += edits(ref_c, hyp_c)
                        g["chars"] += len(ref_c)
                        g["n"] += 1
                        g["exact"] += canonical(text) == ref_c
                    for kind, (hit, total) in entity_hits(r["question"], text, terms).items():
                        g["entities"][kind][0] += hit
                        g["entities"][kind][1] += total
            if config == "paraformer-2pass+hotwords":
                text = agent_text(hyp, "itn+lexicon", lexicons.get(r["tenant"], Lexicon([])))
                want = {e.value for e in extract_entities(r["question"]) if e.kind == "order_id"}
                got = assess([e for e in extract_entities(text) if e.kind == "order_id"],
                             other_passes=[r.get("streaming") or ""])
                if want and got:
                    flagged = any(e.confidence < 0.6 for e in got)
                    right = {e.value for e in got} == want
                    stats = readback[r["condition"]]
                    stats["right" if right else "wrong"] += 1
                    stats["right_flagged" if right else "wrong_flagged"] += flagged
            if config in ("paraformer-2pass+hotwords", "sensevoice", "whisper+prompt") and not respelled(r):
                for term in terms:
                    if term in r["question"] and term not in hyp and _latin(term):
                        aliases[term][_aligned(r["question"], hyp, term)] += 1
    table = []
    for (config, cond, variant, mode), g in sorted(groups.items()):
        row = {"config": config, "condition": cond, "variant": variant, "text": mode}
        if mode == "itn+lexicon":
            row.update(n=g["n"], cer=round(g["edits"] / g["chars"], 4) if g["chars"] else None,
                       exact=round(g["exact"] / g["n"], 4) if g["n"] else None)
        row["entities"] = {k: {"hit": h, "total": t, "accuracy": round(h / t, 4)} for k, (h, t) in g["entities"].items()}
        table.append(row)
    return {"asr": table,
            "readback": {c: {**s, "recall": round(s["wrong_flagged"] / s["wrong"], 4) if s["wrong"] else None,
                             "false_alarm": round(s["right_flagged"] / s["right"], 4) if s["right"] else None}
                         for c, s in readback.items()},
            "alias_suggestions": {t: dict(sorted(v.items(), key=lambda kv: -kv[1])[:8]) for t, v in aliases.items()}}


def _aligned(reference: str, hypothesis: str, term: str) -> str:
    """The span of the hypothesis that lines up with ``term`` in the reference."""
    start = reference.index(term)
    matcher = difflib.SequenceMatcher(None, reference, hypothesis, autojunk=False)
    lo = hi = None
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if i2 <= start or i1 >= start + len(term):
            continue
        lo = j1 if lo is None else min(lo, j1)
        hi = j2 if hi is None else max(hi, j2)
    return hypothesis[lo:hi].strip() if lo is not None else ""


def latency_tables(records: list[dict], vad: list[dict], tts: list[dict], synth: list[dict]) -> dict:
    asr = {}
    for engine, field in (("paraformer", "final_hw_ms"), ("sensevoice", "final_nohw_ms"), ("whisper", "final_hw_ms")):
        rows = [r for r in records if r["engine"] == engine and r.get(field) is not None]
        if not rows:
            continue
        finals = [r[field] for r in rows]
        rtf = [r[field] / 1000 / r["seconds"] for r in rows if r["seconds"]]
        entry = {"n": len(rows), "final_ms_p50": pct(finals, 0.5), "final_ms_p95": pct(finals, 0.95),
                 "final_rtf_p50": round(statistics.median(rtf), 3) if rtf else None}
        chunks = [c for r in rows for c in (r.get("chunk_ms") or [])]
        if chunks:
            entry.update(chunk_ms_p50=pct(chunks, 0.5), chunk_ms_p95=pct(chunks, 0.95), chunk_audio_ms=600,
                         streaming_rtf_p50=round(statistics.median(chunks) / 600, 3))
        punc = [r["punc_ms"] for r in rows if r.get("punc_ms") is not None]
        if punc:  # what the punctuation step adds to the final pass
            entry.update(punc_n=len(punc), punc_ms_p50=pct(punc, 0.5), punc_ms_p95=pct(punc, 0.95))
        asr[engine] = entry
    endpoint = defaultdict(list)
    for row in vad:
        endpoint[(row["detector"], row["condition"], row["end_silence_ms"])].append(row)
    vad_table = []
    for (detector, cond, silence), rows in sorted(endpoint.items()):
        waits = [r["wait_ms"] for r in rows if r["wait_ms"] is not None and not r["premature"]]
        vad_table.append({"detector": detector, "condition": cond, "end_silence_ms": silence, "n": len(rows),
                          "wait_ms_p50": pct(waits, 0.5), "wait_ms_p95": pct(waits, 0.95),
                          "premature_rate": round(sum(r["premature"] for r in rows) / len(rows), 4),
                          "missed_rate": round(sum(not r["ended"] for r in rows) / len(rows), 4)})
    tts_table = {}
    for engine in sorted({r["engine"] for r in tts}):
        rows = [r for r in tts if r["engine"] == engine]
        tts_table[engine] = {"n": len(rows), "first_chunk_ms_p50": pct([r["first_chunk_ms"] for r in rows], 0.5),
                             "first_chunk_ms_p95": pct([r["first_chunk_ms"] for r in rows], 0.95),
                             "rtf_p50": round(statistics.median([r["rtf"] for r in rows if r["rtf"]]), 3)}
    synth_table = {}
    for engine in sorted({r["engine"] for r in synth}):
        rows = [r for r in synth if r["engine"] == engine]
        synth_table[engine] = {"utterances": len(rows), "audio_minutes": round(sum(r["seconds"] for r in rows) / 60, 1),
                               "rtf_p50": round(statistics.median([r["synth_seconds"] / r["seconds"] for r in rows
                                                                   if r["seconds"]]), 3)}
    return {"asr_latency": asr, "endpoint": vad_table, "tts": tts_table, "synthesis": synth_table}


def markdown(report: dict) -> str:
    lines = ["# Voice evaluation (synthetic speech, GitHub Actions CPU runner)", ""]
    lines += ["## ASR, all variants pooled", "", "| config | condition | n | CER | exact | order id | amount | term |",
              "|---|---|---:|---:|---:|---:|---:|---:|"]
    for row in report["asr"]:
        if row["variant"] != "all" or row["text"] != "itn+lexicon":
            continue
        e = row["entities"]
        acc = lambda k: f"{e[k]['accuracy']:.1%} ({e[k]['hit']}/{e[k]['total']})" if k in e else "–"  # noqa: E731
        lines.append(f"| {row['config']} | {row['condition']} | {row['n']} | {row['cer']:.2%} | {row['exact']:.1%} | "
                     f"{acc('order_id')} | {acc('amount')} | {acc('term')} |")
    lines += ["", "## What ITN and hotword correction add (entity accuracy, clean)", "",
              "| config | text given to the agent | order id | amount | term |", "|---|---|---:|---:|---:|"]
    for row in report["asr"]:
        if row["variant"] == "all" and row["condition"] == "clean":
            e = row["entities"]
            acc = lambda k: f"{e[k]['accuracy']:.1%}" if k in e else "–"  # noqa: E731
            lines.append(f"| {row['config']} | {row['text']} | {acc('order_id')} | {acc('amount')} | {acc('term')} |")
    lines += ["", "## Latency", "", "```json", json.dumps({k: report[k] for k in ("asr_latency", "tts", "synthesis")},
                                                          ensure_ascii=False, indent=1), "```", ""]
    lines += ["| detector | condition | end silence | wait p50 | wait p95 | cut off early | missed |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for row in report["endpoint"]:
        lines.append(f"| {row['detector']} | {row['condition']} | {row['end_silence_ms']} | {row['wait_ms_p50']} | "
                     f"{row['wait_ms_p95']} | {row['premature_rate']:.1%} | {row['missed_rate']:.1%} |")
    lines += ["", "## Read-back of order numbers (Paraformer 2-pass)", "", "```json",
              json.dumps(report["readback"], indent=1), "```", "", "## Alias suggestions (dev set only)", "", "```json",
              json.dumps(report["alias_suggestions"], ensure_ascii=False, indent=1), "```"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr", nargs="*", default=[])
    parser.add_argument("--vad", nargs="*", default=[])
    parser.add_argument("--tts", nargs="*", default=[])
    parser.add_argument("--synth", nargs="*", default=[])
    parser.add_argument("--exclude", default="", help="case ids left out (a test item known to be wrong), comma-separated")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    excluded = {x for x in args.exclude.split(",") if x}

    def rows(patterns: list[str]) -> list[dict]:
        return [r for p in patterns for f in sorted(glob.glob(p)) for r in read_jsonl(Path(f))]

    records = [r for r in rows(args.asr) if r["id"] not in excluded]
    if not records:
        print("no ASR records: did the ASR jobs fail?")
        return 1
    synth = rows([str(Path(d) / "manifest.jsonl") for d in args.synth])
    hotwords = tenant_hotwords()
    report = {"hotwords": hotwords, "excluded": sorted(excluded), **asr_tables(records, hotwords),
              **latency_tables(records, rows(args.vad), rows(args.tts), synth)}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "report.md").write_text(markdown(report), encoding="utf-8")
    print(markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
