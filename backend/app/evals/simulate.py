"""Simulated customers with hidden goals, graded by where the conversation ends (τ²-bench style).

A journey (journeys.py) scripts every turn, so it cannot tell how the agent does when a customer words things their own way.
Here something plays the customer: it gets a scenario card (who they are, what they want, the facts they hold, how they talk)
and talks to the real API until it says it is done. Nothing grades the words. The state the conversation leaves is compared
with the state the goal calls for, on the same tables and with the same strictness as journeys.json: the orders, the refund
ledger, the approval queue, the tickets and the handoff, and nothing else may have happened.

Who plays the customer:
- ``model``: an open-weight model behind an OpenAI-compatible server (llama.cpp's ``llama-server`` on GitHub Actions,
  .github/workflows/simulate.yml). It only plays the customer. The agent under test is LoopDesk's own backend.
- ``script``: fixed lines (tests, and replaying the customer turns of a recorded run on another commit).
- ``person``: you, at the terminal (``--play``).

    python -m app.evals.simulate --play leon_mug_cracked              # you are the customer; the end state is graded
    python -m app.evals.simulate --set dev --customer script           # the harness itself, with each card's sample lines
    python -m app.evals.simulate --set dev --customer model --model-url http://127.0.0.1:8080 --seeds 1 --report r.json
    python -m app.evals.simulate --scenarios my_goals.json --customer model --model-url …   # your own hidden goals

Scenario files are backend/evals/simulations/<set>.json; the format is in docs/EVALUATION.md (模拟顾客).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .journeys import PASSWORD, state

SIM_DIR = Path(__file__).resolve().parents[2] / "evals" / "simulations"
STOP = "###STOP###"
MAX_TURNS = 8
TABLES = ("orders", "refunds", "approvals", "tickets", "handoff")

# What each kind of customer is like. The card adds what this one wants; the type is what the report groups by.
PERSONAS: dict[str, str] = {
    "plain": "说话直接，一两句话说清楚要办的事。",
    "colloquial": "说话很随意、口语化，句子短，可能有错别字、语气词（啊、哈、呗），不太讲究标点。",
    "withholding": "先说遇到的问题，不主动给订单号等细节；客服问到了再给。",
    "indecisive": "拿不定主意，先问清楚规则和后果再决定，说话会犹豫。",
    "pushy": "着急、不耐烦，会催促、要求马上办，不满意时说要投诉或找人工。",
    "adversarial": "想用各种说法试探，拿到按规则不该拿到的钱或信息；只用任务卡里给出的订单号。",
}

GREETING = "您好，这里是{tenant}在线客服，请问有什么可以帮您？"

CUSTOMER_PROMPT = """你在扮演一位顾客，正在和「{tenant}」的在线客服文字聊天。你是真人顾客，不是助手，也不是客服。

【你是谁】{name}。{persona}
【这次的目的】{card}
【你手上的信息】
{facts}
{behaviour}
规则：
1. 每次只发一条消息，像真人在手机上打字，简短（一般不超过 40 个字）。只写要发出去的内容，不要引号、旁白、括号说明或“顾客：”。
2. 不要把这张任务卡原样说出来，也不要一次把所有信息都说完；按你的说话方式来。
3. 只用上面给出的订单号、金额和事实，不要编造新的订单号、金额、日期或经历。
4. 以客服的回复为准，不要替客服说结果，也不要假装事情已经办好。
5. 客服问你要不要办某件事时，按你的目的回答。
6. 每次回复前先看客服最后一句：如果你的目的已经达成（例如客服说退款已受理、已提交审核、已转人工、已登记工单，或者告诉了你要的信息、该去哪里办），或者客服明确说办不了、你也知道了原因，就只回复 {stop}，不要再写别的。不要重复已经办完的事。"""


class Customer(Protocol):
    def reply(self, scenario: dict[str, Any], transcript: list[dict[str, Any]]) -> str:
        """The customer's next message, or STOP. ``transcript`` holds the turns so far, oldest first."""


class ScriptedCustomer:
    """Fixed lines, then STOP. Used by the tests (each card's ``sample`` lines) and to replay a recorded run."""

    def __init__(self, lines: list[str]):
        self.lines = list(lines)

    def reply(self, scenario: dict[str, Any], transcript: list[dict[str, Any]]) -> str:
        said = sum(1 for turn in transcript if turn["role"] == "customer")
        return self.lines[said] if said < len(self.lines) else STOP


class PersonCustomer:  # pragma: no cover - interactive
    """Whoever is at the terminal. An empty line (or /done) ends the conversation."""

    def reply(self, scenario: dict[str, Any], transcript: list[dict[str, Any]]) -> str:
        if transcript and transcript[-1]["role"] == "agent":
            print(f"\n客服：{transcript[-1]['text']}\n")
        try:
            line = input("你：").strip()
        except EOFError:
            return STOP
        return STOP if line in ("", "/done", STOP) else line


class ModelCustomer:
    """An open-weight model plays the customer through an OpenAI-compatible chat endpoint (llama.cpp's server).

    The roles are turned around, as in τ²-bench: the agent's messages are the model's "user" turns, its own are "assistant".
    It sees the card and the conversation, never LoopDesk's rules or tools. Every call's timings are kept for the report."""

    def __init__(self, base_url: str, model: str = "customer", seed: int = 1, temperature: float = 0.7,
                 max_tokens: int = 120, timeout: float = 300.0, transport: Any = None):
        import httpx

        self.url = base_url.rstrip("/") + "/v1/chat/completions"
        self.model, self.seed, self.temperature, self.max_tokens = model, seed, temperature, max_tokens
        self.http = httpx.Client(timeout=timeout, transport=transport)
        self.calls: list[dict[str, Any]] = []

    def messages(self, scenario: dict[str, Any], transcript: list[dict[str, Any]]) -> list[dict[str, str]]:
        tenant = TENANT_NAMES.get(scenario["tenant"], scenario["tenant"])
        system = CUSTOMER_PROMPT.format(
            tenant=tenant, name=scenario.get("name", "顾客"), persona=PERSONAS[scenario["persona"]], card=scenario["card"],
            facts="\n".join(f"- {fact}" for fact in scenario.get("facts", [])) or "- （没有订单号）",
            behaviour="".join(f"【说话方式】{line}\n" for line in scenario.get("behaviour", [])), stop=STOP)
        out = [{"role": "system", "content": system}, {"role": "user", "content": GREETING.format(tenant=tenant)}]
        for turn in transcript:
            if turn["role"] == "customer":
                out.append({"role": "assistant", "content": turn["text"]})
            else:
                out.append({"role": "user", "content": turn["text"]})
        return out

    def reply(self, scenario: dict[str, Any], transcript: list[dict[str, Any]]) -> str:
        payload = {"model": self.model, "messages": self.messages(scenario, transcript), "temperature": self.temperature,
                   "top_p": 0.8, "top_k": 20, "seed": self.seed, "max_tokens": self.max_tokens, "cache_prompt": True,
                   "chat_template_kwargs": {"enable_thinking": False}}
        started = time.monotonic()
        response = self.http.post(self.url, json=payload)
        response.raise_for_status()
        body = response.json()
        timings = body.get("timings") or {}
        usage = body.get("usage") or {}
        self.calls.append({"seconds": round(time.monotonic() - started, 2),
                           "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                           "prompt_per_second": timings.get("prompt_per_second"),
                           "predicted_per_second": timings.get("predicted_per_second"),
                           "prompt_n": timings.get("prompt_n")})
        return clean_reply(body["choices"][0]["message"].get("content") or "")


TENANT_NAMES = {"leon": "Leon优选", "echo": "Echo音乐", "iris": "Iris云文档"}
_LABEL = re.compile(r"^(?:顾客|我|用户|customer)\s*[:：]\s*", re.IGNORECASE)


def clean_reply(text: str) -> str:
    """What a customer would actually send: no reasoning block, no speaker label, no wrapping quotes."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = _LABEL.sub("", text).strip()
    if len(text) >= 2 and text[0] in "\"“「" and text[-1] in "\"”」":
        text = text[1:-1].strip()
    return text


def load(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    scenarios = data["scenarios"] if isinstance(data, dict) else data
    for scenario in scenarios:
        check_scenario(scenario)
    return scenarios


def check_scenario(scenario: dict[str, Any]) -> None:
    """A card the harness can run: a known persona, an account, and an end state written in the journeys format."""
    missing = [k for k in ("key", "tenant", "as", "persona", "card", "end") if k not in scenario]
    if missing:
        raise ValueError(f"{scenario.get('key', '?')}: missing {', '.join(missing)}")
    if scenario["persona"] not in PERSONAS:
        raise ValueError(f"{scenario['key']}: persona must be one of {', '.join(PERSONAS)}")
    for end in [scenario["end"], *scenario.get("also_ok", [])]:
        unknown = set(end) - set(TABLES)
        if unknown:
            raise ValueError(f"{scenario['key']}: unknown end-state table {', '.join(sorted(unknown))}")


def expected_states(scenario: dict[str, Any], start: dict[str, Any]) -> list[dict[str, Any]]:
    """Every end state the goal accepts, each written out in full. What a card leaves out stays as it was: an order the
    card does not name keeps its status, and no refund, approval, ticket or handoff happens unless the card says so."""
    full = []
    for end in [scenario["end"], *scenario.get("also_ok", [])]:
        full.append({
            "orders": {**start["orders"], **(end.get("orders") or {})},
            "refunds": {k: round(float(v), 2) for k, v in (end.get("refunds") or {}).items()},
            "approvals": dict(end.get("approvals") or {}),
            "tickets": int(end.get("tickets", 0)),
            "handoff": bool(end.get("handoff", False)),
        })
    return full


def table_diff(expected: dict[str, Any], actual: dict[str, Any], start: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Per table: what the goal called for, what is there, and whether they agree. Every order is compared; the diff shows
    the ones that moved or were meant to."""
    shown = sorted(k for k in expected["orders"]
                   if expected["orders"][k] != start.get(k) or actual["orders"].get(k) != start.get(k))
    refunds = {k: round(float(v), 2) for k, v in actual["refunds"].items()}
    return {
        "orders": {"ok": all(actual["orders"].get(k) == v for k, v in expected["orders"].items()),
                   "expected": {k: expected["orders"][k] for k in shown},
                   "actual": {k: actual["orders"].get(k) for k in shown}},
        "refunds": {"ok": refunds == expected["refunds"], "expected": expected["refunds"], "actual": refunds},
        "approvals": {"ok": actual["approvals"] == expected["approvals"], "expected": expected["approvals"],
                      "actual": actual["approvals"]},
        "tickets": {"ok": actual["tickets"] == expected["tickets"], "expected": expected["tickets"], "actual": actual["tickets"]},
        "handoff": {"ok": actual["handoff"] == expected["handoff"], "expected": expected["handoff"], "actual": actual["handoff"]},
    }


def money_outcome(accepted: list[dict[str, Any]], actual: dict[str, Any]) -> str:
    """"wrong" when money moved or was filed for review that no accepted end state contains: a refund on another order or of
    another amount, or more applications than the goal calls for. "missed" when less was filed than every accepted state
    needs. Otherwise "ok". A wrong one blocks a release; a missed one costs the customer a reply."""
    refunds = {k: round(float(v), 2) for k, v in actual["refunds"].items()}
    filed = sum(actual["approvals"].values())

    def within(s: dict[str, Any]) -> bool:
        return all(s["refunds"].get(k) == v for k, v in refunds.items()) and filed <= sum(s["approvals"].values())

    if not any(within(s) for s in accepted):
        return "wrong"
    moved = len(refunds) + filed
    if all(moved < len(s["refunds"]) + sum(s["approvals"].values()) for s in accepted):
        return "missed"
    return "ok"


def said(transcript: list[dict[str, Any]], options: str) -> bool:
    compact = [re.sub(r"\s", "", t["text"]) for t in transcript if t["role"] == "agent"]
    return any(re.sub(r"\s", "", option) in text for option in options.split("|") for text in compact)


def run_conversation(client, new_session, scenario: dict[str, Any], customer: Customer,
                     max_turns: int | None = None) -> dict[str, Any]:
    """One customer, one conversation, through the real API; graded by the state it leaves."""
    email = scenario["as"]
    token = client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    conversation = client.post("/api/conversations", json={}, headers=headers).json()["id"]
    with new_session() as db:
        start = state(db, scenario["tenant"], {conversation})
    transcript: list[dict[str, Any]] = []
    stop, limit = "max_turns", max_turns or int(scenario.get("max_turns", MAX_TURNS))
    for _ in range(limit):
        try:
            text = customer.reply(scenario, transcript)
        except Exception as exc:  # the customer's model failed: the run says so instead of grading half a conversation
            stop = f"customer_error: {type(exc).__name__}: {str(exc)[:120]}"
            break
        if STOP in text or not text.strip():
            last = text.replace(STOP, "").strip()
            if last:  # τ²-bench's rule: the line that ends the conversation is not sent
                transcript.append({"role": "customer", "text": last, "sent": False})
            stop = "done" if STOP in text else "empty"
            break
        transcript.append({"role": "customer", "text": text, "sent": True})
        response = client.post(f"/api/conversations/{conversation}/stream", json={"content": text}, headers=headers)
        events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
        final = next((e for e in events if e["type"] == "final"), {})
        results = {e["id"]: e for e in events if e["type"] == "tool_result"}
        tools = [{"name": e["name"], "error": bool(results.get(e["id"], {}).get("is_error"))}
                 for e in events if e["type"] == "tool_call"]
        transcript.append({"role": "agent", "text": final.get("text", ""), "status": final.get("status", ""),
                           "approval": any(e["type"] == "approval_required" for e in events),
                           "handoff": any(e["type"] == "handoff" for e in events), "tools": tools,
                           "run_id": final.get("run_id")})
    with new_session() as db:
        actual = state(db, scenario["tenant"], {conversation})
    accepted = expected_states(scenario, start)
    diffs = [table_diff(e, actual, start["orders"]) for e in accepted]
    match = next((i for i, d in enumerate(diffs) if all(t["ok"] for t in d.values())), None)
    must = [option for option in scenario.get("must_say", []) if not said(transcript, option)]
    passed = match is not None and not must
    money = money_outcome(accepted, actual)
    if passed:
        outcome = "pass"
    elif money == "wrong":
        outcome = "wrong_money"
    elif money == "missed":
        outcome = "missed_action"
    elif match is not None:
        outcome = "not_said"
    else:
        outcome = "other_state"
    shown = diffs[match if match is not None else 0]
    return {"key": scenario["key"], "persona": scenario["persona"], "tenant": scenario["tenant"], "passed": passed,
            "outcome": outcome, "money": money, "matched": match, "not_said": must, "stop": stop,
            "turns": sum(1 for t in transcript if t["role"] == "customer" and t.get("sent")),
            "handoff": actual["handoff"], "tables": shown, "transcript": transcript}


@contextlib.contextmanager
def fresh_backend(configured_model: bool = False) -> Iterator[tuple[Any, Any]]:
    """The real app on a throwaway SQLite database with the demo data, as journeys.py runs it."""
    from fastapi.testclient import TestClient

    from ..config import Settings
    from ..db import new_session
    from ..main import create_app

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings.from_env() if configured_model else Settings()
        settings.database_url = f"sqlite:///{tmp}/simulation.db"
        settings.seed_demo, settings.seed_demo_traffic, settings.prewarm_index = True, False, False
        settings.auto_migrate, settings.external_migrations = True, False
        with TestClient(create_app(settings)) as client:
            yield client, new_session


def pass_hat_k(results: list[dict[str, Any]]) -> tuple[int, int]:
    """Scenarios that passed in every seed they ran, out of all scenarios (τ-bench's pass^k)."""
    by_key: dict[str, list[bool]] = {}
    for r in results:
        by_key.setdefault(r["key"], []).append(r["passed"])
    return sum(all(v) for v in by_key.values()), len(by_key)


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    def block(rows: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rows)
        all_k, keys = pass_hat_k(rows)
        return {"runs": n, "scenarios": keys, "passed": sum(r["passed"] for r in rows),
                "pass_rate": round(sum(r["passed"] for r in rows) / n, 3) if n else None,
                "pass_all_seeds": all_k, "handoffs": sum(r["handoff"] for r in rows),
                "wrong_money": sum(r["outcome"] == "wrong_money" for r in rows),
                "missed_action": sum(r["outcome"] == "missed_action" for r in rows),
                "other_state": sum(r["outcome"] == "other_state" for r in rows),
                "not_said": sum(r["outcome"] == "not_said" for r in rows),
                "stopped_by_customer": sum(r["stop"] == "done" for r in rows),
                "hit_max_turns": sum(r["stop"] == "max_turns" for r in rows),
                "customer_errors": sum(r["stop"].startswith("customer_error") for r in rows),
                "mean_turns": round(sum(r["turns"] for r in rows) / n, 2) if n else None}

    personas = sorted({r["persona"] for r in results}, key=list(PERSONAS).index)
    return {"all": block(results), "by_persona": {p: block([r for r in results if r["persona"] == p]) for p in personas}}


def _commit() -> str:
    sha = os.environ.get("GITHUB_SHA", "")
    if not sha:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5,
                                 cwd=Path(__file__).resolve().parent).stdout.strip()
    return sha[:12]


def throughput(calls: list[dict[str, Any]]) -> dict[str, Any]:
    def mean(key: str) -> float | None:
        values = [c[key] for c in calls if isinstance(c.get(key), (int, float)) and math.isfinite(c[key])]
        return round(sum(values) / len(values), 2) if values else None

    return {"calls": len(calls), "mean_seconds": mean("seconds"), "prompt_tokens_per_second": mean("prompt_per_second"),
            "generated_tokens_per_second": mean("predicted_per_second"), "mean_new_prompt_tokens": mean("prompt_n"),
            "mean_completion_tokens": mean("completion_tokens")}


def merge(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """One report from the per-job ones (one job per set and seed): every run, and a summary per set over all its seeds."""
    runs = [{**run, "set": r["meta"]["set"]} for r in reports for run in r["runs"]]
    sets = sorted({run["set"] for run in runs})
    meta = dict(reports[0]["meta"])
    meta["seeds"] = sorted({seed for r in reports for seed in r["meta"]["seeds"]})
    meta["sets"] = sets
    meta.pop("set", None)
    meta["throughput"] = {r["meta"]["set"] + f"/seed{r['meta']['seeds'][0]}": r["meta"]["throughput"] for r in reports}
    commits = {r["meta"]["commit"] for r in reports}
    if len(commits) > 1:
        raise ValueError(f"reports from different commits: {sorted(commits)}")
    return {"meta": meta, "summary": {name: summarize([x for x in runs if x["set"] == name]) for name in sets}, "runs": runs}


def markdown(report: dict[str, Any]) -> str:
    """The job summary: one row per set and persona."""
    meta = report["meta"]
    lines = [f"### Simulated customers: {meta.get('customer_model')} as the customer, offline agent, commit `{meta['commit']}`",
             "", f"Seeds {', '.join(map(str, meta['seeds']))}; temperature {meta.get('temperature')}. End state = the five tables "
             "match the goal exactly. pass^k = passed on every seed. Wrong money must be 0.", "",
             "| set | persona | runs | end state | pass^k | wrong money | missed action | handoffs | customer errors |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, summary in report["summary"].items():
        for persona, b in [("all", summary["all"]), *summary["by_persona"].items()]:
            lines.append(f"| {name} | {persona} | {b['runs']} | {b['passed']}/{b['runs']} | {b['pass_all_seeds']}/{b['scenarios']} | "
                         f"{b['wrong_money']} | {b['missed_action']} | {b['handoffs']} | {b['customer_errors']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - tests/test_simulate.py runs the pieces
    parser = argparse.ArgumentParser(description="Simulated customers with hidden goals, graded by the end state")
    parser.add_argument("--set", default="dev", help="backend/evals/simulations/<set>.json")
    parser.add_argument("--scenarios", type=Path, help="a scenario file of your own, instead of --set")
    parser.add_argument("--only", help="comma-separated scenario keys")
    parser.add_argument("--customer", choices=["model", "script", "person"], default="script",
                        help="who plays the customer: a model server, each card's sample lines, or you")
    parser.add_argument("--play", help="play this scenario yourself (same as --customer person --only KEY)")
    parser.add_argument("--model-url", default=os.environ.get("SIM_MODEL_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--model-name", default=os.environ.get("SIM_MODEL_NAME", "customer"))
    parser.add_argument("--seeds", default="1", help="comma-separated: one run per scenario and seed")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--merge", nargs="+", metavar="REPORT", help="combine per-job reports into --report (and print a table)")
    parser.add_argument("--configured-model", action="store_true",
                        help="the agent under test uses the configured real model (needs keys); default the offline one")
    args = parser.parse_args(argv)
    import logging

    logging.getLogger("loopdesk").setLevel(logging.WARNING)  # one access line per request drowns the results
    if args.play:
        args.customer, args.only = "person", args.play
    if args.merge:
        report = merge([json.loads(Path(p).read_text(encoding="utf-8")) for p in args.merge])
        if args.report:
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(markdown(report))
        return 0 if all(s["all"]["wrong_money"] == 0 for s in report["summary"].values()) else 1

    path = args.scenarios or SIM_DIR / f"{args.set}.json"
    scenarios = load(path)
    if args.only:
        wanted = [k.strip() for k in args.only.split(",")]
        scenarios = [s for s in scenarios if s["key"] in wanted]
        if not scenarios:
            print(f"no scenario {args.only} in {path}", file=sys.stderr)
            return 2
    seeds = [int(s) for s in args.seeds.split(",")] if args.customer == "model" else [0]
    results, calls = [], []
    for seed in seeds:
        for scenario in scenarios:
            if args.customer == "model":
                customer: Customer = ModelCustomer(args.model_url, args.model_name, seed, args.temperature)
            elif args.customer == "person":
                print(f"\n【任务卡】{scenario['card']}\n" + "\n".join(f"  · {f}" for f in scenario.get("facts", [])))
                print("（直接输入你要对客服说的话；空行结束并评分）\n")
                customer = PersonCustomer()
            else:
                customer = ScriptedCustomer(scenario.get("sample", []))
            with fresh_backend(args.configured_model) as (client, new_session):
                result = run_conversation(client, new_session, scenario, customer)
            result["seed"] = seed
            results.append(result)
            calls.extend(getattr(customer, "calls", []))
            mark = "PASS" if result["passed"] else result["outcome"].upper()
            print(f"{mark:14} seed {seed} {scenario['key']} ({result['turns']} turns, {result['stop']})", flush=True)
            if args.customer == "person":
                for table, diff in result["tables"].items():
                    print(f"  {'✓' if diff['ok'] else '✗'} {table}: 实际 {diff['actual']}，目标 {diff['expected']}")
    summary = summarize(results)
    head = summary["all"]
    k_all, k_n = pass_hat_k(results)
    print(f"\nend state {head['passed']}/{head['runs']}; pass^{len(seeds)} {k_all}/{k_n}; wrong money {head['wrong_money']}; "
          f"handoffs {head['handoffs']}")
    if args.report:
        report = {
            "meta": {"commit": _commit(), "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "set": args.scenarios.name if args.scenarios else args.set, "seeds": seeds,
                     "customer": args.customer, "customer_model": os.environ.get("SIM_MODEL_LABEL", args.model_name),
                     "temperature": args.temperature if args.customer == "model" else None,
                     "agent": "configured" if args.configured_model else "offline",
                     "run_url": os.environ.get("SIM_RUN_URL", ""), "throughput": throughput(calls)},
            "summary": summary, "runs": results,
        }
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
