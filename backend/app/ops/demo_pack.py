"""Build the data the static demo's browser engine answers from (frontend/src/demo/engine/pack.json).

The engine (frontend/src/demo/engine) answers questions nobody recorded, in the visitor's browser, without a
server. It must not become a second, weaker copy of the product's rules, so everything that can be data is
exported from the backend's own modules here: the regular expressions behind the money rules, the planner's
texts, the demo tenants' orders and refund limits, the chunks the ingest pipeline made (flagged ones left out),
and a word list for segmenting Chinese. Only the logic is written twice, and tests/test_demo_pack.py and
frontend/src/demo/engine/gates.test.ts hold it to the backend's results.

Timestamps are stored as hours before "now", resolved in the browser: after-sales windows are judged against
the visitor's clock, as seed.py does for a fresh database.

    python -m app.ops.demo_pack ../frontend/src/demo/engine/pack.json
    python -m app.ops.demo_pack --check ../frontend/src/demo/engine/pack.json   # fails when it is out of date
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

# Words of jieba's dictionary kept for segmenting questions (the knowledge base's own words are always kept). The
# whole dictionary is 3.6 MB; at this cut-off nearly every word a shopper types is still there, and an unknown run
# of characters stays one unknown word, as jieba's HMM would leave it, so the relevance gate still counts it.
MIN_WORD_FREQ = 300
MIN_COMPOUND_FREQ = 3
_CST = timezone(timedelta(hours=8))
_HAN = re.compile(r"[\u4e00-\u9fff]+")


def _flags(pattern: re.Pattern[str]) -> str:
    return "i" if pattern.flags & re.IGNORECASE else ""


def js_pattern(pattern: re.Pattern[str]) -> dict[str, str]:
    """The expression as JavaScript reads it: no possessive quantifiers (the one that has them only ever sees a
    string of 16 characters, so backtracking is bounded), and astral code points in the u form."""
    source = pattern.pattern.replace("*+", "*").replace("++", "+")
    flags = _flags(pattern)
    if "\\U" in source:
        source = re.sub(r"\\U([0-9A-Fa-f]{8})", lambda m: "\\u{" + m.group(1).lstrip("0") + "}", source)
        flags += "u"
    return {"source": source, "flags": flags}


def _module_rules(module: ModuleType) -> dict[str, dict[str, str]]:
    return {name: js_pattern(value) for name, value in vars(module).items() if isinstance(value, re.Pattern)}


def _module_texts(module: ModuleType) -> dict[str, str]:
    return {name: value for name, value in vars(module).items()
            if isinstance(value, str) and name.isupper() and not name.startswith("__")}


def rules() -> dict[str, Any]:
    from ..agent import business_tools, handoff, outcome, rewrite, verifier
    from ..guardrails import injection, pii
    from ..llm import mock
    from ..voice import confirm
    from .. import textutil

    return {
        "text": {**_module_rules(textutil), "ORDER_ID_RE": js_pattern(textutil.ORDER_ID_RE._re())},
        "verifier": _module_rules(verifier),
        "confirm": _module_rules(confirm),
        "mock": _module_rules(mock),
        "rewrite": _module_rules(rewrite),
        "tools": {"_FAULT": js_pattern(business_tools._FAULT)},
        "handoff": _module_rules(handoff),
        "outcome": {"REFUSAL_RE": js_pattern(outcome.REFUSAL_RE)},
        "injection": [[name, js_pattern(pattern)] for name, pattern in injection._MESSAGE_PATTERNS],
        "pii": [[name, js_pattern(pattern)] for name, pattern in pii._RULES],
    }


def texts() -> dict[str, Any]:
    from ..agent import business_tools, handoff, rewrite
    from ..llm import mock
    from ..voice import confirm
    from .. import textutil

    return {
        "mixed_words": [word for word in textutil.DOMAIN_WORDS if not _HAN.fullmatch(word)],
        "yes_words": sorted(confirm._YES),
        "ba_ok": sorted(confirm._BA_OK),
        "mock": _module_texts(mock),
        "tools": {**_module_texts(business_tools), "STATUS_TEXT": business_tools.STATUS_TEXT,
                  "PRIORITY_TEXT": business_tools.PRIORITY_TEXT, "REFUND_STATUS_TEXT": business_tools.REFUND_STATUS_TEXT},
        "handoff": _module_texts(handoff),
        "query_synonyms": textutil.QUERY_SYNONYMS,
        "stop_tokens": sorted(textutil.STOP_TOKENS),
        "typos": [[pattern.pattern, right] for pattern, right in textutil.TYPOS],
        "leading": list(rewrite._LEADING),
    }


def _hours_ago(when: datetime | None, now: datetime) -> int | None:
    """Whole hours: seed.py counts in hours, so the file does not change with the moment it was built."""
    if when is None:
        return None
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return round((now - when).total_seconds() / 3600)


def _event_hours_ago(event: dict[str, Any], now: datetime) -> dict[str, Any]:
    when = datetime.strptime(event["time"], "%Y-%m-%d %H:%M").replace(tzinfo=_CST)
    return {"ago_h": round(max(0.0, (now - when).total_seconds() / 3600)), "desc": event["desc"]}


def word_list(chunks: list[str]) -> str:
    """"word bucket word bucket …": the bucket is twice the log2 of jieba's frequency, all the segmenter needs."""
    import jieba

    from ..textutil import DOMAIN_WORDS, tokenize

    jieba.initialize()
    words = {word: freq for word, freq in jieba.dt.FREQ.items() if freq >= MIN_WORD_FREQ and _HAN.fullmatch(word)}
    known: set[str] = set()
    for text in chunks:
        for word in [*tokenize(text), *jieba.lcut(text)]:
            if _HAN.fullmatch(word) and jieba.dt.FREQ.get(word, 0) > 0:
                words.setdefault(word, jieba.dt.FREQ[word])
                if len(word) > 1:
                    known.add(word)
    # A rarer compound around a word the knowledge base uses: "货到付款" must stay one word the knowledge base has
    # never seen, or its 付款 alone would answer the question with the payment policy.
    for word, freq in jieba.dt.FREQ.items():
        if freq >= MIN_COMPOUND_FREQ and len(word) > 2 and _HAN.fullmatch(word) and any(word[i:i + 2] in known for i in range(len(word) - 1)):
            words.setdefault(word, freq)
    for word in DOMAIN_WORDS:
        if _HAN.fullmatch(word):
            # add_word gives them the least frequency that keeps them whole: rounding must not undo that
            words[word] = 2 * jieba.dt.FREQ.get(word, 1)
    words.pop("货运费", None)
    return " ".join(f"{word}{round(2 * math.log2(max(freq, 1)))}" for word, freq in sorted(words.items()))


def script_map() -> str:
    """Traditional -> simplified, as pairs of characters, from the same table the backend reads input with."""
    from opencc import OpenCC

    t2s = OpenCC("t2s")
    pairs: dict[str, str] = {}
    for code in range(0x4E00, 0xA000):
        char = chr(code)
        simple = t2s.convert(char)
        if simple != char and len(simple) == 1 and ord(simple) < 0x10000:  # the table is read two UTF-16 units at a time
            pairs[char] = simple
    return "".join(f"{a}{b}" for a, b in sorted(pairs.items()))


def build() -> dict[str, Any]:
    from sqlalchemy import select

    from ..config import Settings
    from ..db import create_all, init_engine, new_session
    from ..models import Chunk, Document, Tenant, User
    from ..seed import fixture_orders, fixture_refunds, seed_demo
    from ..services import Services, set_services

    settings = Settings()
    with tempfile.TemporaryDirectory() as tmp:
        settings.database_url = f"sqlite:///{tmp}/pack.db"
        init_engine(settings.database_url)
        create_all()
        services = Services(settings)
        set_services(services)
        out: dict[str, Any] = {"tenants": {}, "accounts": {}, "orders": {}, "refunds": {}, "kb": {}, "tools": {}, "members": {}}
        now = datetime.now(timezone.utc)
        chunks_text: list[str] = []
        with new_session() as db:
            seed_demo(db, services.embedder)
            for tenant in db.execute(select(Tenant).order_by(Tenant.id)).scalars():
                slug = tenant.slug
                out["tenants"][slug] = {
                    "name": tenant.name, "industry": tenant.industry, "auto_refund_limit": tenant.auto_refund_limit,
                    "approval_admin_threshold": tenant.approval_admin_threshold, "ticket_sla_hours": tenant.ticket_sla_hours,
                    "return_window_days": tenant.return_window_days, "quality_window_days": tenant.quality_window_days,
                    "official_domains": tenant.official_domains or [],
                }
                users = db.execute(select(User).where(User.tenant_id == tenant.id).order_by(User.id)).scalars().all()
                by_email = {u.email: u for u in users}
                email_of = {u.id: u.email for u in users}
                for user in users:
                    out["accounts"][user.email] = {"id": user.id, "name": user.name, "role": user.role, "tenant": slug}
                out["orders"][slug] = [{
                    "id": o.id, "customer": email_of[o.customer_id], "amount": o.amount, "status": o.status, "items": o.items,
                    "region": o.region or "domestic", "paid_ago_h": _hours_ago(o.paid_at, now), "delivered_ago_h": _hours_ago(o.delivered_at, now),
                    "shipment": ({"carrier": o.shipment["carrier"], "tracking_no": o.shipment["tracking_no"],
                                  "events": [_event_hours_ago(e, now) for e in o.shipment["events"]]} if o.shipment else None),
                } for o in fixture_orders(slug, by_email)]
                out["refunds"][slug] = [{
                    "order_id": r.order_id, "customer": email_of[r.customer_id], "amount": r.amount, "status": r.status,
                    "ago_h": _hours_ago(r.created_at, now), "reason": r.reason,
                } for r in fixture_refunds(slug, by_email)]
                rows = db.execute(select(Chunk, Document.title, Document.visibility).join(Document, Document.id == Chunk.document_id)
                                  .where(Chunk.tenant_id == tenant.id).order_by(Chunk.id)).all()
                # A flagged chunk is never retrieved (hybrid_search): the demo does not need to carry it.
                out["kb"][slug] = [{"id": c.id, "doc": title, "doc_id": c.document_id, "heading": c.heading, "text": c.text,
                                    "visibility": visibility} for c, title, visibility in rows if not c.flagged_injection]
                chunks_text += [f"{c.heading}\n{c.text}" for c, _, _ in rows]
                pack_dir = Path(__file__).resolve().parents[2] / "tenant_packs" / slug
                if pack_dir.exists():
                    # What the planner reads; where a tool's calls go and the name of their secret stay out.
                    out["tools"][slug] = [{k: v for k, v in tool.items() if k != "binding"}
                                          for tool in json.loads((pack_dir / "tools.json").read_text(encoding="utf-8"))]
                    merchant = json.loads((pack_dir / "merchant.json").read_text(encoding="utf-8"))
                    out["members"][slug] = merchant["collections"]["members"]
    from .. import textutil
    from ..agent import business_tools

    out["settings"] = {"timing_reach": textutil._TIMING_REACH, "bare_demand_max": textutil._BARE_DEMAND_MAX, "plain_demand_max": textutil._PLAIN_DEMAND_MAX,
                       "my_orders_limit": business_tools.MY_ORDERS_LIMIT, "rejected_before_days": business_tools.REJECTED_BEFORE_DAYS,
                       "min_relevance": settings.min_relevance, "retrieval_top_k": settings.retrieval_top_k,
                       "refund_window_days": settings.refund_window_days, "refund_window_max_count": settings.refund_window_max_count,
                       "refund_window_max_amount": settings.refund_window_max_amount, "max_agent_steps": settings.max_agent_steps}
    out["rules"] = rules()
    out["texts"] = texts()
    out["words"] = word_list(chunks_text)
    out["t2s"] = script_map()
    return out


# Phrases that decide whether money moves: every short string of the backend's test suite and gate cases that talks
# about refunds, with what the backend's own functions say about it. gates.test.ts holds the browser's port to this.
_MONEY_WORDS = re.compile(r"退|refund|money back|撤回|撤销|取消|钱|人工|投诉")


def _item_names(pack: dict[str, Any]) -> list[str]:
    """Every item on sale in the demo: the browser cannot tell which order a clause is about until it has read it."""
    return sorted({item["name"] for orders in pack["orders"].values() for order in orders for item in order["items"]})


def money_phrases(items: list[str] | None = None) -> dict[str, list[Any]]:
    from .. import textutil as t
    from ..voice.confirm import classify_reply

    backend = Path(__file__).resolve().parents[2]
    found: set[str] = set()
    for path in sorted((backend / "tests").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and 2 < len(node.value) < 80:
                if "\n" not in node.value and _MONEY_WORDS.search(node.value):
                    found.add(node.value)
    for name in ("leon_default.jsonl", "echo_default.jsonl", "iris_default.jsonl", "leon_hard.jsonl"):
        for line in (backend / "evals" / name).read_text(encoding="utf-8").splitlines():
            found.add(json.loads(line)["question"])
    review = json.loads((backend / "evals" / "money_review_lists.json").read_text(encoding="utf-8"))
    found.update(review["adversarial"], review["plain"], (turn for case in review["multi_turn"] for turn in case["turns"]))
    items = items or []
    return {text: [t.refund_requested(text, items), t.refund_declined(text, items), t.cancel_requested(text, items),
                   t.refund_withdraw_requested(text), t.withdraw_confirmed(text), t.refund_bare_decline(text),
                   t.stated_refund_amount(text), t.asks_for_person(text), t.handoff_cancelled(text)[0], classify_reply(text)]
            for text in sorted(found)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the static demo's data pack")
    parser.add_argument("output", type=Path)
    parser.add_argument("--check", action="store_true", help="exit 1 when the committed files are out of date")
    args = parser.parse_args(argv)
    pack = build()
    phrases = money_phrases(_item_names(pack))
    phrases_file = args.output.with_name("money_phrases.json")
    if args.check:
        current = json.loads(args.output.read_text(encoding="utf-8"))
        if current != pack or json.loads(phrases_file.read_text(encoding="utf-8")) != phrases:
            print(f"{args.output} is out of date: python -m app.ops.demo_pack {args.output}")
            return 1
        return 0
    args.output.write_text(json.dumps(pack, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    rows = ",\n".join(f"{json.dumps(text, ensure_ascii=False)}: {json.dumps(row)}" for text, row in phrases.items())
    phrases_file.write_text(f"{{\n{rows}\n}}\n", encoding="utf-8")  # one phrase a line: a rule change shows as that phrase changing
    print(f"{args.output}: {args.output.stat().st_size // 1024} KB, {len(pack['words'].split())} words")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
