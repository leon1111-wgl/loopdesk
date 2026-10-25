"""Report for the Cantonese and code-switching measurement (yue_run.py). Pure Python (and OpenCC's tables).

    python -m app.evals.voice.yue_report --rows 'out/yue-*/*.jsonl' --out out/report

Per set and engine setting:
- token error rate: Chinese characters and English words are the tokens (so "iPhone" is one error, not
  six), both sides folded to Simplified (an engine that writes 機 for a reference's 机 is not wrong), with
  the same numeral canonicalisation as the Mandarin scoring;
- numbers in the reference found in the hypothesis, and amounts after ITN (Cantonese 蚊 and 皮 included):
  the entities the agent's tools depend on;
- English words of the reference found in the hypothesis (code-switching);
- which script the engine writes (share of hypotheses in Traditional among those with a script-specific
  character), and compute time (RTF on the runner).
FLEURS references are written Cantonese read aloud; colloquial words an engine writes in standard
written Chinese (他们 for 佢哋) count as errors, which is what a Cantonese customer's transcript is
scored against here.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from ...voice.itn import canonical, extract_entities, itn
from .public_report import numbers


@lru_cache(maxsize=2)
def _converter(config: str):
    from opencc import OpenCC

    return OpenCC(config)


def simplified(text: str) -> str:
    return _converter("t2s").convert(text)


def is_traditional(text: str) -> bool | None:
    """True / False when the text has a character that differs between the scripts; None when not."""
    if simplified(text) != text:
        return True
    return False if _converter("s2t").convert(text) != text else None


_CJK = re.compile(r"[㐀-䶿一-鿿]+")
_RUN = re.compile(r"[㐀-䶿一-鿿]+|[a-z0-9'%.]+")


def tokens(text: str) -> list[str]:
    t = simplified(unicodedata.normalize("NFKC", text or "").lower())
    out: list[str] = []
    for m in _RUN.finditer(t):
        piece = m.group(0).strip(".'")
        if not piece:
            continue
        if _CJK.fullmatch(piece):
            out.extend(canonical(piece))  # numerals become digits, one token each
        elif re.fullmatch(r"[\d.%]+", piece):
            out.extend(c for c in piece if c.isdigit())
        else:
            out.append(piece)
    return out


def _distance(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def _amounts(text: str) -> collections.Counter:
    return collections.Counter(e.value for e in extract_entities(itn(simplified(text)).text) if e.kind == "amount")


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))]


def table(records: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for r in records:
        groups[(r["variant"], r["config"])].append(r)
    rows = []
    for (variant, config), items in sorted(groups.items()):
        errors = ref_tokens = sentence_errors = 0
        num_total = num_found = amount_total = amount_found = en_total = en_found = 0
        scripts, ms = [], []
        for r in items:
            ref, hyp = tokens(r["question"]), tokens(r["hyp"])
            d = _distance(ref, hyp)
            errors, ref_tokens, sentence_errors = errors + d, ref_tokens + len(ref), sentence_errors + (d > 0)
            ref_numbers, hyp_numbers = collections.Counter(numbers(simplified(r["question"]))), \
                collections.Counter(numbers(simplified(r["hyp"])))
            num_total += sum(ref_numbers.values())
            num_found += sum((ref_numbers & hyp_numbers).values())
            ra, ha = _amounts(r["question"]), _amounts(r["hyp"])
            amount_total += sum(ra.values())
            amount_found += sum((ra & ha).values())
            ref_en = collections.Counter(t for t in ref if re.fullmatch(r"[a-z][a-z']*", t))
            en_total += sum(ref_en.values())
            en_found += sum((ref_en & collections.Counter(hyp)).values())
            if (script := is_traditional(r["hyp"])) is not None:
                scripts.append(script)
            ms.append(r["ms"])
        seconds = sum(r["seconds"] for r in items) or 1.0
        rows.append({"set": variant, "config": config, "n": len(items), "ter": errors / max(1, ref_tokens),
                     "sentence_error": sentence_errors / len(items),
                     "numbers": num_total, "number_accuracy": num_found / num_total if num_total else None,
                     "amounts": amount_total, "amount_accuracy": amount_found / amount_total if amount_total else None,
                     "english_words": en_total, "english_recall": en_found / en_total if en_total else None,
                     "traditional_share": sum(scripts) / len(scripts) if scripts else None,
                     "errors": sum(1 for r in items if r.get("error")),
                     "ms_p50": _percentile(ms, 0.5), "ms_p95": _percentile(ms, 0.95), "rtf": sum(ms) / 1000 / seconds})
    return rows


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def render(rows: list[dict]) -> str:
    lines = ["# Cantonese and code-switching (measure only)", "",
             "| set | engine setting | n | token error | sentence error | numbers | amounts | English words | "
             "writes Traditional | ms p50 / p95 | RTF | failed |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| {r['set']} | {r['config']} | {r['n']} | {_pct(r['ter'])} | {_pct(r['sentence_error'])} | "
                     f"{_pct(r['number_accuracy'])} ({r['numbers']}) | {_pct(r['amount_accuracy'])} ({r['amounts']}) | "
                     f"{_pct(r['english_recall'])} ({r['english_words']}) | {_pct(r['traditional_share'])} | "
                     f"{r['ms_p50'] or 0:.0f} / {r['ms_p95'] or 0:.0f} | {r['rtf']:.2f} | {r['errors']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    records = []
    for path in sorted(glob.glob(args.rows)):
        with open(path, encoding="utf-8") as fh:
            records.extend(json.loads(line) for line in fh if line.strip())
    rows = table(records)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.md").write_text(render(rows), encoding="utf-8")
    (args.out / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
