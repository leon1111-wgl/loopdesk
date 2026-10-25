"""Report for semantic endpointing (turn_run.py). Pure Python; the curves need matplotlib.

    python -m app.evals.voice.turn_report --rows 'out/turn-*/*.jsonl' --out out/report

Per corpus, condition and configuration:
- truncation: the first end decision came before the speaker had finished (a pause mid-sentence);
- wait p50 / p95: from the true end of speech to the decision, plus the compute time of the Smart Turn call
  that decided it, over the utterances that were not cut;
- undetected: no end decision at all within 3.5 s of the end (babble keeps the VAD "in speech");
- with a take-back window W (the time from the end decision to the answer's first sentence reaching TTS):
  an end followed by speech within W is withdrawn and merged (voice/session.py), so only a later end
  counts; "withdrawn" is how often that happened per utterance (a run thrown away).

Protocol, fixed before the first run: configurations are compared on AISHELL-1's dev speakers only
(clean). The choice is the deployable configuration with the lowest truncation at W = 0 whose p50 wait
is at most 600 ms (ties: lower p95). That one configuration, and today's default next to it, are then
reported on the held-out sets: AISHELL-1's test speakers, FLEURS cmn (another corpus, other microphones)
and the synthetic set. The default changes only if the held-out numbers support it.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
from pathlib import Path

from ...voice.endpoint import SAMPLE_RATE

DEPLOYABLE = ("silence-energy", "silence-silero", "rules", "smart_turn")  # smart_turn_only is an ablation
DEFAULT = "silence-energy-600"  # LOOPDESK_VOICE_VAD=energy, LOOPDESK_VOICE_END_SILENCE_MS=600
DEV = "aishell1-dev"
HELD_OUT = ("aishell1-test", "fleurs-cmn-test", "synthetic")
WINDOWS = (0, 500, 1000, 1500)
TARGET_TRUNCATION, TARGET_P50 = 0.03, 600.0
FAMILY_LABELS = {"silence-energy": "Silence (energy VAD)", "silence-silero": "Silence (Silero VAD)",
                 "rules": "Silence + text rule", "smart_turn": "Smart Turn v3 + text rule",
                 "smart_turn_only": "Smart Turn v3 alone"}


def _ms(samples: float) -> float:
    return samples / SAMPLE_RATE * 1000


def outcome(row: dict, window_ms: float) -> dict:
    """The end that stands: the first one not followed by speech within ``window_ms`` (taken back)."""
    window = window_ms / 1000 * SAMPLE_RATE
    starts, merges = row["starts"], 0
    for at, reason, ms in row["ends"]:
        resumed = next((s for s in starts if s > at), None)
        if window and resumed is not None and resumed - at < window:
            merges += 1
            continue
        premature = at < row["true_end"]
        return {"ended": True, "premature": premature, "merges": merges, "reason": reason,
                "wait_ms": None if premature else _ms(at - row["true_end"]) + (ms or 0.0)}
    return {"ended": False, "premature": False, "merges": merges, "reason": None, "wait_ms": None}


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def summarize(rows: list[dict], windows=WINDOWS) -> dict:
    """{(group, condition, config, window): metrics}; order-number utterances also as group "<group>+order_id"."""
    acc: dict = collections.defaultdict(lambda: {"n": 0, "started": 0, "premature": 0, "ended": 0, "merges": 0,
                                                 "waits": [], "family": None})
    for r in rows:
        groups = [r["group"]] + ([f"{r['group']}+order_id"] if r.get("order_id") else [])
        for w in windows:
            o = outcome(r, w) if r.get("started", True) else None
            for g in groups:
                a = acc[(g, r["condition"], r["config"], w)]
                a["family"] = r["family"]
                a["n"] += 1
                if o is None:
                    continue
                a["started"] += 1
                a["ended"] += o["ended"]
                a["premature"] += o["premature"]
                a["merges"] += o["merges"]
                if o["wait_ms"] is not None:
                    a["waits"].append(o["wait_ms"])
    table = {}
    for key, a in acc.items():
        started = a["started"] or 1
        table[key] = {"family": a["family"], "n": a["n"], "started": a["started"],
                      "truncation": a["premature"] / started, "undetected": (a["started"] - a["ended"]) / started,
                      "withdrawn": a["merges"] / started,
                      "wait_p50_ms": _percentile(a["waits"], 0.5), "wait_p95_ms": _percentile(a["waits"], 0.95)}
    return table


def dev_keys(table: dict, dev: str = DEV, condition: str = "clean", window: int = 0) -> list[tuple]:
    return [k for k in table if k[0] == dev and k[1] == condition and k[3] == window]


def select(table: dict, dev: str = DEV, max_p50_ms: float = TARGET_P50, window: int = 0,
           families=DEPLOYABLE) -> str | None:
    """The configuration chosen on the dev set alone; None when none meets the latency bar."""
    candidates = []
    for key in dev_keys(table, dev, "clean", window):
        m = table[key]
        if m["family"] not in families or m["wait_p50_ms"] is None or m["wait_p50_ms"] > max_p50_ms:
            continue
        candidates.append((m["truncation"], m["wait_p95_ms"] or 0.0, m["wait_p50_ms"], key[2]))
    return min(candidates)[3] if candidates else None


def best_per_family(table: dict, group: str, condition: str = "clean", window: int = 0,
                    max_p50_ms: float = TARGET_P50) -> dict[str, str]:
    best: dict[str, tuple] = {}
    for key, m in table.items():
        if key[0] != group or key[1] != condition or key[3] != window or m["wait_p50_ms"] is None:
            continue
        if m["wait_p50_ms"] > max_p50_ms:
            continue
        candidate = (m["truncation"], m["wait_p95_ms"] or 0.0, key[2])
        if m["family"] not in best or candidate < best[m["family"]]:
            best[m["family"]] = candidate
    return {family: c[2] for family, c in best.items()}


def classifier(asks: list[dict], thresholds=(0.3, 0.5, 0.7, 0.9)) -> dict:
    """Smart Turn as a classifier of pauses: an ask before the true end is a pause mid-sentence (the right
    answer is "not finished"), one after it is the real end. Plus the compute time per call."""
    out: dict = {}
    grouped: dict = collections.defaultdict(list)
    for a in asks:
        grouped[(a["group"], a["condition"])].append(a)
    for key, items in grouped.items():
        mid = [p for a in items for pos, p, _ in a["asks"] if pos < a["true_end"]]
        end = [p for a in items for pos, p, _ in a["asks"] if pos >= a["true_end"]]
        ms = [m for a in items for _, _, m in a["asks"]]
        view = {"ms_p50": _percentile(ms, 0.5), "ms_p95": _percentile(ms, 0.95), "calls": len(ms)}
        for t in thresholds:
            view[t] = {"mid_pauses": len(mid), "ends": len(end),
                       "mid_said_incomplete": sum(p < t for p in mid) / len(mid) if mid else None,
                       "end_said_complete": sum(p >= t for p in end) / len(end) if end else None}
        out[key] = view
    return out


# ---------------------------------------------------------------- the report
def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _msf(x: float | None) -> str:
    return "—" if x is None else f"{x:.0f}"


def _line(table: dict, key: tuple, label: str | None = None) -> str:
    m = table.get(key)
    if m is None:
        return f"| {label or key[2]} | — | — | — | — | — | — |"
    return (f"| {label or key[2]} | {m['started']} | {_pct(m['truncation'])} | {_msf(m['wait_p50_ms'])} | "
            f"{_msf(m['wait_p95_ms'])} | {_pct(m['undetected'])} | {_pct(m['withdrawn'])} |")


HEADER = "| configuration | n | truncated | wait p50 ms | wait p95 ms | undetected | withdrawn |\n|---|---:|---:|---:|---:|---:|---:|"


def render(table: dict, classes: dict) -> tuple[str, dict]:
    chosen = select(table, window=0)
    chosen_w = select(table, window=1000)
    lines = ["# Semantic endpointing", "",
             "Protocol: chosen on AISHELL-1 dev speakers (clean) only; held-out sets reported once. "
             "Truncation target ≤3%, wait p50 ≤600 ms.", ""]
    lines += ["## Dev (AISHELL-1 dev speakers, clean): best of each family with p50 ≤ 600 ms", "", "### W = 0 (no take-back)",
              "", HEADER]
    best = best_per_family(table, DEV)
    for family in DEPLOYABLE + ("smart_turn_only",):
        if family in best:
            lines.append(_line(table, (DEV, "clean", best[family], 0), f"{FAMILY_LABELS[family]}: `{best[family]}`"))
    lines.append(_line(table, (DEV, "clean", DEFAULT, 0), f"today's default: `{DEFAULT}`"))
    lines += ["", f"**Chosen on dev (W = 0): `{chosen}`**" if chosen else "**Nothing on dev meets p50 ≤ 600 ms.**",
              f"Chosen on dev if a take-back window of 1 s is assumed: `{chosen_w}`" if chosen_w else "", ""]
    verdict = {}
    for config, label in ((chosen, "chosen at W = 0"), (chosen_w if chosen_w != chosen else None, "chosen at W = 1 s")):
        if not config:
            continue
        lines += [f"## Held out: `{config}` ({label}) vs today's default", ""]
        for window in (0, 1000):
            lines += [f"### W = {window} ms", "", HEADER]
            for group in HELD_OUT:
                for cond in ("clean", "snr10"):
                    for cfg in dict.fromkeys((config, DEFAULT)):
                        if (group, cond, cfg, window) in table:
                            lines.append(_line(table, (group, cond, cfg, window), f"{group} {cond} `{cfg}`"))
            lines.append("")
        held = {g: table.get((g, "clean", config, 0)) for g in ("aishell1-test", "fleurs-cmn-test")}
        verdict[config] = {g: {"meets_target": bool(m and m["truncation"] <= TARGET_TRUNCATION and m["wait_p50_ms"] is not None
                                                    and m["wait_p50_ms"] <= TARGET_P50), **(m or {})} for g, m in held.items()}
    lines += ["## Order-number utterances (synthetic, clean)", "", HEADER]
    for cfg in dict.fromkeys(filter(None, (chosen, chosen_w, DEFAULT))):
        for window in (0, 1000):
            lines.append(_line(table, ("synthetic+order_id", "clean", cfg, window), f"`{cfg}` W={window}"))
    lines += ["", "## Babble (SNR 10 dB): undetected ends, best of each family", "", HEADER]
    for group in ("synthetic", "aishell1-test"):
        for family, cfg in sorted(best_per_family(table, group, "snr10", 0, max_p50_ms=10_000).items()):
            lines.append(_line(table, (group, "snr10", cfg, 0), f"{group} {FAMILY_LABELS[family]}: `{cfg}`"))
    lines += ["", "## Smart Turn as a pause classifier", "",
              "| set | condition | threshold | mid-sentence pauses | said not finished | real ends | said finished | ms p50 / p95 |",
              "|---|---|---:|---:|---:|---:|---:|---:|"]
    for (group, cond), view in sorted(classes.items()):
        for t in (0.3, 0.5, 0.7, 0.9):
            v = view.get(t)
            if v:
                lines.append(f"| {group} | {cond} | {t} | {v['mid_pauses']} | {_pct(v['mid_said_incomplete'])} | "
                             f"{v['ends']} | {_pct(v['end_said_complete'])} | {_msf(view['ms_p50'])} / {_msf(view['ms_p95'])} |")
    return "\n".join(lines) + "\n", {"chosen": chosen, "chosen_w1000": chosen_w, "held_out": verdict}


def plot(table: dict, out: Path, window: int) -> None:
    """Truncation against wait (p50 top row, p95 bottom row), one panel per set, one line per family: each
    family's lower-left frontier over its sweep. Static PNG for the docs; the numbers are in the tables."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"silence-energy": "#2a78d6", "silence-silero": "#eb6834", "rules": "#1baf7a", "smart_turn": "#6250d6"}
    groups = [(DEV, "AISHELL-1 dev (tuning)"), ("aishell1-test", "AISHELL-1 test (held out)"),
              ("fleurs-cmn-test", "FLEURS cmn (held out)"), ("synthetic", "Synthetic TTS")]
    fig, axes = plt.subplots(2, 4, figsize=(16, 7.5), sharey="row")
    for row, stat in enumerate(("wait_p50_ms", "wait_p95_ms")):
        for col, (group, title) in enumerate(groups):
            ax = axes[row][col]
            limit = TARGET_P50 if stat == "wait_p50_ms" else None
            if limit:
                ax.axvspan(0, limit, ymin=0, ymax=1, color="#e9e8e4", zorder=0)
                ax.axhline(TARGET_TRUNCATION * 100, color="#8a8984", lw=1, ls="--", zorder=1)
            for family, color in colors.items():
                points = sorted((m[stat], m["truncation"] * 100) for k, m in table.items()
                                if k[0] == group and k[1] == "clean" and k[3] == window and m["family"] == family
                                and m[stat] is not None)
                frontier, best = [], float("inf")
                for x, y in points:
                    if y < best:
                        frontier.append((x, y))
                        best = y
                if not frontier:
                    continue
                ax.scatter([p[0] for p in points], [p[1] for p in points], s=10, color=color, alpha=0.25, lw=0, zorder=2)
                ax.plot([p[0] for p in frontier], [p[1] for p in frontier], color=color, lw=2, marker="o", ms=4,
                        label=FAMILY_LABELS[family], zorder=3)
            m = table.get((group, "clean", DEFAULT, window))
            if m and m[stat] is not None:
                ax.scatter([m[stat]], [m["truncation"] * 100], s=70, facecolors="none", edgecolors="#0b0b0b", lw=1.5, zorder=4)
                ax.annotate("default", (m[stat], m["truncation"] * 100), xytext=(6, 6), textcoords="offset points",
                            fontsize=8, color="#52514e")
            ax.set_xlim(0, 3200 if stat == "wait_p95_ms" else 2200)
            ax.grid(color="#e9e8e4", lw=0.6)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            ax.tick_params(colors="#52514e", labelsize=8)
            if row == 0:
                ax.set_title(title, fontsize=10, color="#0b0b0b")
            ax.set_xlabel("wait p50 (ms)" if stat == "wait_p50_ms" else "wait p95 (ms)", fontsize=9, color="#52514e")
            if col == 0:
                ax.set_ylabel("truncated utterances (%)", fontsize=9, color="#52514e")
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=4, frameon=False, fontsize=9)
    fig.suptitle(f"Endpointing: truncation vs wait (clean, take-back window {window} ms; grey: target p50 ≤ 600 ms, "
                 f"dashed: 3%)", y=0.02, fontsize=9, color="#52514e")
    fig.tight_layout(rect=(0, 0.04, 1, 0.94))
    fig.savefig(out, dpi=130)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", required=True, help="glob of turn_run row files (not the -asks files)")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args(argv)
    rows, asks = [], []
    for path in sorted(glob.glob(args.rows)):
        target = asks if path.endswith("-asks.jsonl") else rows
        with open(path, encoding="utf-8") as fh:
            target.extend(json.loads(line) for line in fh if line.strip())
    table = summarize(rows)
    classes = classifier(asks)
    text, verdict = render(table, classes)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.md").write_text(text, encoding="utf-8")
    flat = [{"group": k[0], "condition": k[1], "config": k[2], "window_ms": k[3], **m} for k, m in sorted(table.items())]
    (args.out / "summary.json").write_text(json.dumps({"verdict": verdict, "table": flat,
                                                       "classifier": {f"{g}|{c}": v for (g, c), v in classes.items()}},
                                                      ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if not args.no_plot:
        for window in (0, 1000):
            plot(table, args.out / f"endpointing-w{window}.png", window)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
