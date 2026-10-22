"""Task success by voice versus by text, on the ASR transcripts the voice-eval workflow produced.

    python -m app.evals.voice.agent_eval --asr 'data/asr-*.jsonl' --config paraformer-2pass+hotwords+punc \\
        --variants kokoro-zf_001-1 --conditions clean,snr5 --report agent.json

``--config`` is one of the report's configurations (app/evals/voice/report.py CONFIGS): an engine and
the transcript field it is scored on; ``--field`` alone takes that field from every record given.

The agent and the scoring are the regression suite's (app/evals/runner.py): the same cases, the
same sandbox and checks, dry-run tools. The only difference is the input: the text question
versus what the voice channel would hand the agent (app/voice/session.py understand(): ITN,
hotword correction, PII masking) and the voice channel's confirmation gate in front of money
tools. A read-back is answered by a simulated customer who knows the case's intent: "确认" when
the case asks for a refund, "取消" otherwise.

Attribution of every case that passes by text and fails by voice:
- asr_entity: an order number, amount or plan name in the question did not come through;
- asr_other: the transcript differs from the question in some other way;
- agent: the transcript matches the question (canonical text): the agent itself varied.
Uses whichever model LOOPDESK_LLM_* configures; offline-model numbers say nothing about model quality.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import statistics
import unicodedata
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select

from ...agent.runtime import AgentRuntime, RunOptions, collect
from ...integrations import Commerce
from ...models import Conversation, EvalCase, Tenant
from ...voice.confirm import VOICE_CONFIRM_ERROR, Confirmations, gate_registry
from ...voice.engines import ASRResult
from ...voice.itn import canonical, extract_entities
from ...voice.lexicon import lexicon_for
from ...voice.session import understand
from ..runner import _resolve_user, build_sandbox, score_case
from .dataset import read_jsonl
from .report import CONFIGS, DECODED_FROM, as_received, unique


class _Run:
    """One run, and how long until its first sentence reached the customer."""

    def __init__(self, events):
        self.started = time.perf_counter()
        self.first_sentence_ms = None
        self.events = self._timed(events)

    def _timed(self, events):
        for event in events:
            if self.first_sentence_ms is None and event["type"] in ("token", "final"):
                self.first_sentence_ms = (time.perf_counter() - self.started) * 1000
            yield event


def _blocked(summary: dict) -> int:
    return sum(1 for r in summary["tool_results"]
               if r.get("is_error") and (r.get("output") or {}).get("error") == VOICE_CONFIRM_ERROR)


def _without_blocked(summary: dict) -> dict:
    """A call the confirmation gate stopped never ran: it is not a tool call for scoring."""
    blocked = [r for r in summary["tool_results"]
               if r.get("is_error") and (r.get("output") or {}).get("error") == VOICE_CONFIRM_ERROR]
    calls = list(summary["tool_calls"])
    for r in blocked:
        calls.remove(r["name"])
    return {**summary, "tool_calls": calls, "tool_results": [r for r in summary["tool_results"] if r not in blocked]}


class Evaluator:
    def __init__(self, db, services):
        self.db, self.services = db, services
        self.local = threading.local()
        self.cases = {(t.slug, c.key): c for t in db.execute(select(Tenant)).scalars()
                      for c in db.execute(select(EvalCase).where(EvalCase.tenant_id == t.id, EvalCase.suite == "default",
                                                                 EvalCase.status == "active")).scalars()}
        self.lock = threading.Lock()

    def _sandbox(self, slug: str):
        cache = getattr(self.local, "sandboxes", None)
        if cache is None:
            cache = self.local.sandboxes = {}
        if slug not in cache:
            with self.lock:
                tenant = self.db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one()
                sandbox = build_sandbox(self.db, tenant)
            cache[slug] = (sandbox, sandbox.get(Tenant, tenant.id), lexicon_for(sandbox, sandbox.get(Tenant, tenant.id)))
        return cache[slug]

    def _start(self, sandbox, tenant, user, text, conversation, registry=None):
        s = self.services
        runtime = AgentRuntime(sandbox, s.settings, s.provider, s.embedder, registry or s.registry, s.breaker,
                               Commerce.local(), reranker=getattr(s, "reranker", None))
        run = _Run(runtime.start(tenant=tenant, user=user, text=text, conversation=conversation,
                                 options=RunOptions(dry_run=True)))
        summary = collect(run.events)
        return summary, run.first_sentence_ms

    def _conversation(self, sandbox, tenant, user, case):
        if not case.history:
            return None
        conversation = Conversation(tenant_id=tenant.id, user_id=user.id, channel="eval")
        sandbox.add(conversation)
        sandbox.commit()
        for earlier in case.history:
            self._start(sandbox, tenant, user, earlier, conversation)
        return conversation

    def text(self, slug: str, key: str) -> dict:
        sandbox, tenant, _ = self._sandbox(slug)
        case = self.cases[(slug, key)]
        user = _resolve_user(sandbox, tenant, case)
        summary, first = self._start(sandbox, tenant, user, case.question, self._conversation(sandbox, tenant, user, case))
        passed, checks, answer, tools = score_case(case, summary)
        return {"passed": passed, "checks": checks, "first_sentence_ms": first, "answer": answer}

    def voice(self, record: dict, hyp: str, decoded: str | None = None) -> dict:
        slug, key = record["tenant"], record["key"]
        sandbox, tenant, lexicon = self._sandbox(slug)
        case = self.cases[(slug, key)]
        user = _resolve_user(sandbox, tenant, case)
        transcript = understand(as_received(hyp, decoded, record.get("streaming") or hyp), lexicon)
        conversation = self._conversation(sandbox, tenant, user, case)
        confirmations = Confirmations()
        registry = gate_registry(self.services.registry, confirmations, lambda: transcript.text)
        summary, first = self._start(sandbox, tenant, user, transcript.text, conversation, registry)
        blocked = _blocked(summary)
        confirmed = None
        if confirmations.pending is not None:
            wants_refund = "request_refund" in (case.expected_tools or []) or bool(case.expect_approval)
            confirmed = wants_refund  # False: the model tried a refund the customer did not ask for
            if wants_refund:
                pending = confirmations.accept()
                second, _ = self._start(sandbox, tenant, user, f"确认：{pending.utterance}", conversation, registry)
                summary = {"events": summary["events"] + second["events"],
                           "tool_calls": summary["tool_calls"] + second["tool_calls"],
                           "tool_results": summary["tool_results"] + second["tool_results"],
                           "approval": second["approval"] or summary["approval"],
                           "verification": second["verification"], "final": second["final"], "done": second["done"]}
        passed, checks, answer, tools = score_case(case, _without_blocked(summary))
        want = {(e.kind, e.value) for e in extract_entities(case.question, lexicon.terms)}
        got = {(e.kind, e.value) for e in extract_entities(transcript.text, lexicon.terms)}
        unsure = [e.to_dict() for e in transcript.entities if e.kind in ("order_id", "amount")
                  and e.confidence < self.services.settings.voice_readback_threshold]
        return {"passed": passed, "checks": checks, "first_sentence_ms": first, "answer": answer,
                "transcript": transcript.text, "same_as_text": canonical(transcript.text) == canonical(case.question),
                "identical": re.sub(r"\s", "", unicodedata.normalize("NFKC", transcript.text))
                == re.sub(r"\s", "", unicodedata.normalize("NFKC", case.question)),
                "entities_missing": sorted(f"{k}:{v}" for k, v in want - got), "readback": unsure,
                "confirm_blocked": blocked, "confirmed": confirmed,
                # A wrong refund attempt the spoken confirmation stopped: by text the same call would have
                # run. Such a pass is the gate's, not the transcript's, and is reported separately.
                "gate_stopped_wrong_refund": confirmed is False}


def attribute(text_passed: bool, voice: dict) -> str | None:
    if voice["passed"]:
        return None
    if not text_passed:
        return "agent_also_fails_by_text"
    if voice["entities_missing"]:
        return "asr_entity"
    if voice["same_as_text"]:
        # Same words. If only punctuation differs, the ASR's missing commas changed the outcome (the
        # offline agent splits "退款，另外发票怎么开" on the comma); otherwise the agent itself varied.
        return "agent" if voice["identical"] else "asr_punctuation"
    return "asr_other"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr", nargs="+", required=True)
    parser.add_argument("--config", choices=sorted(CONFIGS), help="an engine and its transcript field (sets --field)")
    parser.add_argument("--field", default="final_hw")
    parser.add_argument("--variants", default="")
    parser.add_argument("--conditions", default="clean")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--exclude", default="", help="case ids left out (a test item known to be wrong), comma-separated")
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)

    from ...config import get_settings
    from ...db import create_all, init_engine, new_session
    from ...seed import seed_demo
    from ...services import Services, set_services

    engine, field = CONFIGS[args.config] if args.config else (None, args.field)
    settings = get_settings()
    tmp = tempfile.TemporaryDirectory()
    settings.database_url = f"sqlite:///{Path(tmp.name) / 'voice-agent.db'}"
    init_engine(settings.database_url)
    create_all()
    services = Services(settings)
    set_services(services)
    variants = {v for v in args.variants.split(",") if v}
    conditions = set(args.conditions.split(","))
    excluded = {x for x in args.exclude.split(",") if x}
    records = [r for p in args.asr for f in sorted(glob.glob(p)) for r in read_jsonl(Path(f))
               if r.get(field) is not None and (engine is None or r.get("engine") == engine) and r["condition"] in conditions
               and (not variants or r["variant"] in variants) and r["id"] not in excluded]
    records = unique(records)
    with new_session() as db:
        seed_demo(db, services.embedder)
        evaluator = Evaluator(db, services)
        keys = sorted({(r["tenant"], r["key"]) for r in records if (r["tenant"], r["key"]) in evaluator.cases})
        with ThreadPoolExecutor(args.workers) as pool:
            text = dict(zip(keys, pool.map(lambda k: evaluator.text(*k), keys)))
            records = [r for r in records if (r["tenant"], r["key"]) in text]
            voice = list(pool.map(lambda r: evaluator.voice(r, r[field], r.get(DECODED_FROM.get(field, field))), records))
    results, by_condition = [], defaultdict(lambda: defaultdict(int))
    for record, v in zip(records, voice):
        t = text[(record["tenant"], record["key"])]
        cause = attribute(t["passed"], v)
        results.append({"id": record["id"], "variant": record["variant"], "condition": record["condition"],
                        "text_passed": t["passed"], **v, "cause": cause})
        stats = by_condition[record["condition"]]
        stats["n"] += 1
        stats["voice_passed"] += v["passed"]
        stats["text_passed"] += t["passed"]
        stats["readback_triggered"] += bool(v["readback"])
        stats["money_calls_stopped_for_confirmation"] += v["confirm_blocked"]
        stats["wrong_refunds_stopped_by_the_gate"] += v["gate_stopped_wrong_refund"]
        stats["voice_passed_only_because_the_gate_stopped_a_wrong_refund"] += v["passed"] and v["gate_stopped_wrong_refund"]
        if cause:
            stats[f"cause_{cause}"] += 1
        if cause in ("asr_entity", "asr_other") and v["readback"]:
            stats["asr_failures_a_readback_would_catch"] += 1
    firsts_text = [t["first_sentence_ms"] for t in text.values() if t["first_sentence_ms"] is not None]
    firsts_voice = [v["first_sentence_ms"] for v in voice if v["first_sentence_ms"] is not None]

    def pct(values, q):
        ordered = sorted(values)
        return round(ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))], 1) if ordered else None

    summary = {
        "model": f"{settings.llm_provider}:{settings.llm_model_strong}", "config": args.config, "field": field,
        "variants": sorted(variants), "cases": len(keys), "excluded": sorted(excluded),
        "text_pass_rate": round(sum(t["passed"] for t in text.values()) / len(text), 4) if text else None,
        "by_condition": {c: {**s, "voice_pass_rate": round(s["voice_passed"] / s["n"], 4),
                             "text_pass_rate_same_cases": round(s["text_passed"] / s["n"], 4)}
                         for c, s in by_condition.items()},
        "llm_first_sentence_ms": {"text_p50": pct(firsts_text, 0.5), "text_p95": pct(firsts_text, 0.95),
                                  "voice_p50": pct(firsts_voice, 0.5), "voice_p95": pct(firsts_voice, 0.95),
                                  "mean_text": round(statistics.mean(firsts_text), 1) if firsts_text else None},
    }
    Path(args.report).write_text(json.dumps({"summary": summary, "text": {f"{k[0]}/{k[1]}": v for k, v in text.items()},
                                             "voice": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
