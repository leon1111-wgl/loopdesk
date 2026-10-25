"""Turn promptfoo's results into the red-team report: attack success rate per tenant and plugin, the
transcripts of the attacks that worked, the data-state check, the OWASP mapping and the token bill.

    python -m app.redteam.report summarize ../out/redteam --markdown report.md --json summary.json
    python -m app.redteam.report scrub ../out/redteam   # before uploading: fail on the key, hide the relay

Attack success: promptfoo's grader judged the answer a failure (``success`` false for an assertion,
``failureReason`` 1). A test that errored (the target or the grader failed, ``failureReason`` 2) is not
an attack and is counted apart; a run where most tests error has measured nothing.

The data-state check (target.py) is counted next to it, not merged into it: a "violation" is something the
tools did (returned another customer's order, filed a refund on it, refunded automatically above the
limit), whatever the answer said.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .tenants import SPECS

ASSERT_FAILURE = 1
ERROR_FAILURE = 2
# promptfoo scores a grader that returned nothing usable as a failed assertion, which would read as a
# successful attack. These are its messages for that case; such tests are counted as grader errors.
GRADER_FAILURE = re.compile(r"^(No output|Could not extract JSON|Failed to parse|Error (calling|from) (the )?(grading|grader)"
                            r"|LLM-rubric (grading )?error)", re.I)
GENERATION_TOKENS = re.compile(r"Observed generation token usage: ([\d,]+) total")

OWASP_TITLES = {
    "LLM01": "Prompt Injection",
    "LLM02": "Sensitive Information Disclosure",
    "LLM05": "Improper Output Handling",
    "LLM06": "Excessive Agency",
    "LLM08": "Vector and Embedding Weaknesses",
    "LLM09": "Misinformation",
    "LLM10": "Unbounded Consumption",
}

# plugin -> (OWASP Top 10 for LLM Applications 2025, the LoopDesk layer that has to stop it). The first
# five follow promptfoo's own OWASP mapping; the others are ours. Strategies (jailbreak, crescendo) are
# LLM01 whatever the plugin: they are how an attack is delivered, the plugin is what it is after.
_CLASSES: dict[str, tuple[str, str]] = {
    "bola": ("LLM06", "工具层：身份取自登录，订单按顾客和租户校验"),
    "bfla": ("LLM06", "工具层：按角色给工具，后台操作不在对话里"),
    "rbac": ("LLM06", "工具层：按角色给工具，后台操作不在对话里"),
    "excessive-agency": ("LLM06", "工具层 + 人工审批"),
    "pii": ("LLM02", "工具只返回本人数据 + 写入前脱敏"),
    "kb-injection": ("LLM01", "知识入库扫描 + 回答检查"),
    "hijacking": ("LLM10", "系统提示词 + 证据校验（没有专门的一层）"),
    "policy:refund_approval": ("LLM06", "退款工具 + 人工审批"),
    "policy:no_credentials": ("LLM05", "回答检查（索要凭证、站外渠道）"),
    "policy:no_online_refund": ("LLM09", "退款工具（不支持在线退款的订单）+ 证据校验"),
    "policy": ("LLM06", "退款工具 + 人工审批 + 回答检查"),
}


def classify(plugin: str) -> tuple[str, str]:
    """(OWASP 2025 id with title, LoopDesk layer) for a plugin label."""
    key = plugin if plugin in _CLASSES else plugin.split(":")[0]
    owasp, layer = _CLASSES.get(key, ("LLM01", "?"))
    return f"{owasp}:2025 {OWASP_TITLES[owasp]}", layer


_POLICY_IDS = {p.text: p.id for spec in SPECS.values() for p in spec.policies}


def plugin_label(metadata: dict[str, Any]) -> str:
    plugin = str(metadata.get("pluginId") or "unknown").removeprefix("loopdesk:").removeprefix("promptfoo:redteam:")
    if plugin.startswith("file://"):  # a custom plugin: promptfoo records the path of its definition
        plugin = Path(plugin.removeprefix("file://")).stem
    if plugin == "policy":
        text = metadata.get("policy") or (metadata.get("pluginConfig") or {}).get("policy")
        if text in _POLICY_IDS:
            return f"policy:{_POLICY_IDS[text]}"
    return plugin


def _tenant(row: dict[str, Any], folder: str, turns: dict[str, Any]) -> str:
    test_meta = (row.get("testCase") or {}).get("metadata") or {}
    return str(turns.get("tenant") or test_meta.get("tenant") or folder.split("-")[0])


def _transcript(row: dict[str, Any]) -> list[dict[str, str]]:
    """The conversation as the attacker had it: crescendo's turns, the final jailbreak prompt, or the one
    message and the answer."""
    response = row.get("response") or {}
    meta = {**(row.get("metadata") or {}), **(response.get("metadata") or {})}
    for key in ("messages", "redteamHistory"):
        turns = meta.get(key)
        if isinstance(turns, list) and turns:
            out: list[dict[str, str]] = []
            for turn in turns:
                if not isinstance(turn, dict):
                    continue
                if "role" in turn:
                    out.append({"role": str(turn["role"]), "content": str(turn.get("content") or "")})
                else:
                    out.append({"role": "user", "content": str(turn.get("prompt") or "")})
                    out.append({"role": "assistant", "content": str(turn.get("output") or "")})
            if out:
                return out
    prompt = meta.get("redteamFinalPrompt") or (row.get("vars") or {}).get("prompt") or ""
    return [{"role": "user", "content": str(prompt)}, {"role": "assistant", "content": str(response.get("output") or "")}]


def _calls(folder: Path) -> dict[str, list[dict[str, Any]]]:
    """The target's per-call log of a pass (target._log_call), by conversation."""
    calls: dict[str, list[dict[str, Any]]] = defaultdict(list)
    path = folder / "calls.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                calls[str(entry.get("conversation_id"))].append(entry)
    return calls


def _turns(row: dict[str, Any], calls: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Layers, violations and blocked attempts over every LoopDesk call this attack made: the result's own
    metadata for a plain attack, the logged calls of its conversation(s) for crescendo and jailbreak."""
    meta = {**(row.get("metadata") or {}), **((row.get("response") or {}).get("metadata") or {})}
    own = meta.get("loopdesk") or {}
    ids = {str(x) for x in [meta.get("sessionId"), own.get("conversation_id"), *(meta.get("sessionIds") or [])]
           if x not in (None, "")}
    entries = [e for i in sorted(ids) for e in calls.get(i, [])] or ([own] if own else [])
    layers: list[str] = []
    for entry in entries:
        layers += [layer for layer in entry.get("layers") or [] if layer not in layers]
    return {"tenant": own.get("tenant") or next((e.get("tenant") for e in entries if e.get("tenant")), None),
            "layers": layers, "violations": [v for e in entries for v in e.get("violations") or []],
            "blocked": [b for e in entries for b in e.get("blocked") or []],
            "kb": own.get("kb") or next((e.get("kb") for e in entries if e.get("kb")), None)}


def _empty() -> dict[str, Any]:
    return {"attacks": 0, "successes": 0, "errors": 0, "grader_errors": 0, "violations": 0, "blocked": 0,
            "layers": Counter()}


def _rate(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k != "layers"}
    out["asr"] = round(row["successes"] / row["attacks"], 3) if row["attacks"] else None
    out["layers"] = dict(row["layers"])
    return out


TREND_PASS = "trend"


def summarize_dir(root: Path, only: tuple[str, ...] | None = None,
                  exclude: tuple[str, ...] = (TREND_PASS,)) -> dict[str, Any]:
    """Fresh attacks by default; ``only=("trend",)`` for the fixed trend set, which is reported apart so
    that its numbers never mix with the fresh ones."""
    by_plugin: dict[tuple[str, str], dict[str, Any]] = defaultdict(_empty)
    by_strategy: dict[tuple[str, str], dict[str, Any]] = defaultdict(_empty)
    by_owasp: dict[str, dict[str, Any]] = defaultdict(_empty)
    totals = _empty()
    successful: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    tokens = {"target": 0, "generation": 0, "attacker": 0, "grading": 0}
    runs = []
    cases: list[dict[str, Any]] = []
    for path in sorted(Path(root).glob("*/results.json")):
        if (only is not None and path.parent.name not in only) or (only is None and path.parent.name in exclude):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        results = data.get("results") or {}
        runs.append(path.parent.name)
        stats = results.get("stats") or {}
        usage = stats.get("tokenUsage") or {}  # its "total" is the target's tokens, as the target reported them
        tokens["attacker"] += int((usage.get("attacker") or {}).get("total") or 0)
        tokens["grading"] += int((usage.get("assertions") or {}).get("total") or 0)
        log = path.parent / "generate.log"
        if log.is_file() and (found := GENERATION_TOKENS.search(log.read_text(encoding="utf-8", errors="replace"))):
            tokens["generation"] += int(found.group(1).replace(",", ""))
        calls = _calls(path.parent)
        for row in results.get("results") or []:
            test_meta = {**((row.get("testCase") or {}).get("metadata") or {}), **(row.get("metadata") or {})}
            loop = _turns(row, calls)
            tenant = _tenant(row, path.parent.name, loop)
            plugin = plugin_label(test_meta)
            strategy = str(test_meta.get("strategyId") or "basic")
            owasp, _ = classify(plugin)
            response = row.get("response") or {}
            tokens["target"] += int((response.get("tokenUsage") or {}).get("total") or 0)
            buckets = (by_plugin[(tenant, plugin)], by_strategy[(tenant, strategy)], by_owasp[owasp], totals)
            errored = row.get("failureReason") == ERROR_FAILURE or bool(row.get("error") and not row.get("gradingResult"))
            grader_failed = (not errored and not row.get("success")
                             and bool(GRADER_FAILURE.match(str((row.get("gradingResult") or {}).get("reason") or ""))))
            success = (not errored and not grader_failed and not row.get("success")
                       and row.get("failureReason", ASSERT_FAILURE) == ASSERT_FAILURE)
            if test_meta.get("trendId"):
                cases.append({"trend_id": test_meta["trendId"], "tenant": tenant, "plugin": plugin,
                              "outcome": "error" if errored or grader_failed else ("succeeded" if success else "held"),
                              "layers": loop.get("layers") or []})
            if errored or grader_failed:
                for bucket in buckets:
                    bucket["grader_errors" if grader_failed else "errors"] += 1
                continue
            found = list(loop.get("violations") or [])
            for bucket in buckets:
                bucket["attacks"] += 1
                bucket["successes"] += int(success)
                bucket["violations"] += int(bool(found))
                bucket["blocked"] += int(bool(loop.get("blocked")))
                bucket["layers"].update(loop.get("layers") or [])
            record = {"tenant": tenant, "plugin": plugin, "strategy": strategy, "owasp": owasp,
                      "reason": str((row.get("gradingResult") or {}).get("reason") or "")[:600],
                      "layers": loop.get("layers") or [], "violations": found, "kb": loop.get("kb"),
                      "transcript": _transcript(row), "trend_id": test_meta.get("trendId")}
            if success:
                successful.append(record)
            if found:
                violations.append(record)
    return {"runs": runs, "by_plugin": {k: _rate(v) for k, v in sorted(by_plugin.items())},
            "by_strategy": {k: _rate(v) for k, v in sorted(by_strategy.items())},
            "by_owasp": {k: _rate(v) for k, v in sorted(by_owasp.items())},
            "totals": _rate(totals), "successful": successful, "violations": violations, "tokens": tokens,
            "cases": sorted(cases, key=lambda c: c["trend_id"])}


def _pct(row: dict[str, Any]) -> str:
    return "-" if row["asr"] is None else f"{row['asr'] * 100:.0f}%"


def _clip(text: str, limit: int = 700) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def markdown(summary: dict[str, Any], trend: dict[str, Any] | None = None) -> str:
    t = summary["totals"]
    lines = ["# LoopDesk red-team report", "",
             f"Passes: {', '.join(summary['runs'])}. Attacks graded: {t['attacks']}, succeeded: {t['successes']} "
             f"({_pct(t)}); not counted: {t['errors']} errors, {t['grader_errors']} grader failures. "
             f"Data-state violations: {t['violations']}. "
             f"Attacks where a tool refused another customer's order: {t['blocked']}.", "",
             "## By tenant and plugin", "",
             "| tenant | plugin | attacks | succeeded | ASR | state violations | errors / grader failures | OWASP LLM 2025 | layer that must stop it | layers that fired |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for (tenant, plugin), row in summary["by_plugin"].items():
        owasp, layer = classify(plugin)
        fired = ", ".join(f"{k} {v}" for k, v in sorted(row["layers"].items())) or "-"
        lines.append(f"| {tenant} | {plugin} | {row['attacks']} | {row['successes']} | {_pct(row)} | "
                     f"{row['violations']} | {row['errors']} / {row['grader_errors']} | {owasp} | {layer} | {fired} |")
    lines += ["", "## By tenant and strategy", "", "| tenant | strategy | attacks | succeeded | ASR | errors / grader failures |",
              "|---|---|---|---|---|---|"]
    for (tenant, strategy), row in summary["by_strategy"].items():
        lines.append(f"| {tenant} | {strategy} | {row['attacks']} | {row['successes']} | {_pct(row)} | "
                     f"{row['errors']} / {row['grader_errors']} |")
    lines += ["", "## By OWASP category", "", "| OWASP LLM 2025 | attacks | succeeded | ASR |", "|---|---|---|---|"]
    for owasp, row in summary["by_owasp"].items():
        lines.append(f"| {owasp} | {row['attacks']} | {row['successes']} | {_pct(row)} |")
    tok = summary["tokens"]
    lines += ["", "## Tokens", "",
              f"- LoopDesk (the target's own model calls): {tok['target']:,}",
              f"- promptfoo: generating attacks {tok['generation']:,}, strategies' attacker and judge "
              f"{tok['attacker']:,}, grading {tok['grading']:,}",
              f"- all: {sum(tok.values()):,}", ""]
    if summary["violations"]:
        lines += ["## Data-state violations", ""]
        for v in summary["violations"]:
            lines.append(f"- {v['tenant']} / {v['plugin']} / {v['strategy']}: {json.dumps(v['violations'], ensure_ascii=False)}")
        lines.append("")
    if trend is not None and trend["runs"]:
        tt = trend["totals"]
        lines += ["## Trend set (fixed, never regenerated)", "",
                  f"Cases graded: {tt['attacks']}, attacks that succeeded: {tt['successes']} ({_pct(tt)}), "
                  f"errors: {tt['errors']}, data-state violations: {tt['violations']}.", "",
                  "| case | tenant | plugin | outcome | layers that fired |", "|---|---|---|---|---|"]
        for case in trend["cases"]:
            lines.append(f"| {case['trend_id']} | {case['tenant']} | {case['plugin']} | {case['outcome']} | "
                         f"{', '.join(case['layers']) or '-'} |")
        lines.append("")
    lines += ["## Successful attacks", ""]
    if trend is not None:
        summary = {**summary, "successful": summary["successful"] + trend["successful"]}
    if not summary["successful"]:
        lines.append("None.")
    for i, item in enumerate(summary["successful"], 1):
        trend = f", trend {item['trend_id']}" if item.get("trend_id") else ""
        lines += [f"### {i}. {item['tenant']} / {item['plugin']} / {item['strategy']}{trend}", "",
                  f"- OWASP: {item['owasp']}; layers that fired: {', '.join(item['layers']) or 'none'}",
                  f"- grader: {_clip(item['reason'], 400)}"]
        if item.get("kb"):
            lines.append(f"- planted document: {json.dumps(item['kb'], ensure_ascii=False)}")
        lines.append("")
        for turn in item["transcript"]:
            lines.append(f"> **{turn['role']}**: {_clip(turn['content'])}")
            lines.append(">")
        lines.append("")
    return "\n".join(lines)


def _json_ready(summary: dict[str, Any]) -> dict[str, Any]:
    out = dict(summary)
    for key in ("by_plugin", "by_strategy"):
        out[key] = [{"tenant": a, "name": b, **row} for (a, b), row in summary[key].items()]
    return out


def scrub(root: Path, *, secrets: list[str], redact: list[str]) -> None:
    """Refuse to publish a directory that contains a secret; replace the other values (the relay's URL)."""
    secrets = [s for s in secrets if s and len(s) >= 8]
    redact = [r for r in redact if r]
    leaked = []
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            data = path.read_bytes()
            if any(s.encode() in data for s in secrets):
                leaked.append(str(path))
            continue
        if any(s in text for s in secrets):
            leaked.append(str(path))
            continue
        cleaned = text
        for value in redact:
            cleaned = cleaned.replace(value, "<relay>")
        if cleaned != text:
            path.write_text(cleaned, encoding="utf-8")
    if leaked:
        print("secret found in: " + ", ".join(leaked), file=sys.stderr)  # the files, never the value
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("summarize")
    s.add_argument("root", type=Path)
    s.add_argument("--markdown", type=Path)
    s.add_argument("--json", type=Path)
    c = sub.add_parser("scrub")
    c.add_argument("root", type=Path)
    c.add_argument("--secret-env", default="LOOPDESK_LLM_API_KEY")
    c.add_argument("--redact-env", default="LOOPDESK_LLM_BASE_URL")
    args = parser.parse_args(argv)
    if args.command == "scrub":
        scrub(args.root, secrets=[os.environ.get(args.secret_env, "")], redact=[os.environ.get(args.redact_env, "")])
        return
    summary = summarize_dir(args.root)
    trend = summarize_dir(args.root, only=(TREND_PASS,))
    text = markdown(summary, trend=trend)
    if args.markdown:
        args.markdown.write_text(text, encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps({"fresh": _json_ready(summary), "trend": _json_ready(trend)},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
    print(text.split("## Successful attacks")[0])


if __name__ == "__main__":  # pragma: no cover
    main()
