"""Knowledge-version-aware answer cache.

Customer-service traffic is dominated by a long tail of repeated FAQ questions
("退款多久到账？"). Re-running retrieval and the model for each repeat costs tokens and
latency without changing the answer, as long as three things stay the same:

- the knowledge base (any document change, including an applied flywheel patch, changes
  the tenant's index signature);
- the prompt and model configuration (fingerprinted below);
- the visibility scope (staff may see internal documents, customers may not).

All three are part of the cache key, so invalidation is automatic instead of manual.
Only answers that are safe to replay are stored: first question of a conversation (no
dependency on history), knowledge tools only, evidence check passed, no PII, no injection
signals, not a refusal. Matching is exact on a normalised question; semantic (embedding)
matching is deliberately not used, because "退款多久到账" and "退货多久到账" embed close
together but have different answers.

The normalised question is a cache key, not what the agent reads: the router and the tool
rules see the raw text. A customer who typed "我 要 退 款" got a knowledge-base refusal (the
rules did not see 退款) stored under "我要退款", and every other customer's "我要退款" was
answered from it for a day (N-116). So the key folds only what the rules and retrieval ignore
too (width, case, whitespace runs) and keeps spaces and punctuation inside
the question; `cacheable` checks that folding width and case changed no tool signal; and a
refusal is never stored.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import AnswerCache, Tenant, User
from ..rag.retrieval import index_manager
from ..textutil import (ORDER_ID_RE, REFUND_APPLY_RE, REFUND_RE, asks_for_person, normalize_width, refund_declined,
                        refund_requested)
from .outcome import is_refusal
from .prompts import DEFAULT_PROMPT_VERSION, PROMPT_VERSIONS

_SPACES = re.compile(r"\s+")
# Part of the config fingerprint: rows stored under an older normalisation never match (N-116,
# the old key dropped every space, so yesterday's "申请 退款" answer sat under "申请退款").
KEY_FORMAT = "2"


def normalize_question(text: str) -> str:
    """Fold only what the routing rules and retrieval ignore too: full-width letters, digits and
    question marks, case, runs of whitespace ("ＳＯ２０２６  退款？" == "so2026 退款?"). Other punctuation
    and the spaces inside the question are kept (a trailing 。 too: the rules read "…能退货吗。" and
    "…能退货吗" differently): to the rules "申请 退款" is not "申请退款", and "快递 费谁出"
    retrieves other passages than "快递费谁出" (N-116). Fewer hits, never a wrong one."""
    folded = normalize_width(text).replace("？", "?")  # the rules match both question marks alike
    return _SPACES.sub(" ", folded.strip()).lower()


# A second check on what the key does fold (width, case). Words that send a question to a business
# tool (orders, logistics, tickets, a person) rather than to the knowledge base.
_TOOL_WORDS = re.compile(
    r"人工|真人|投诉|转接|订单|单子|快递|物流|包裹|到哪|发货|配送|报修|故障|坏了|工单|我的|我买的"
    r"|human|agent|person|representative|order|track|deliver|ship", re.IGNORECASE)


def signals(text: str) -> frozenset[str]:
    """What the routing rules can see in a question: an order number, a refund or a person asked
    for, a business-tool word."""
    found = {name for name, hit in (
        ("order_id", ORDER_ID_RE.search(text)), ("refund_word", REFUND_RE.search(text)),
        ("refund_apply", REFUND_APPLY_RE.search(text)),
        ("refund_request", refund_requested(text)), ("refund_decline", refund_declined(text)),
        ("person", asks_for_person(text)),
    ) if hit}
    found.update(f"word:{m.group(0).lower()}" for m in _TOOL_WORDS.finditer(text))
    return frozenset(found)


def cacheable(text: str) -> bool:
    """True when the key means what the customer typed: normalising adds or removes no signal."""
    return signals(text) == signals(normalize_question(text))


def config_fingerprint(settings: Settings, prompt_version: str = DEFAULT_PROMPT_VERSION) -> str:
    raw = "|".join([settings.llm_provider, settings.llm_model_strong, settings.llm_model_fast,
                    settings.embedding_provider, settings.embedding_model or "", str(settings.embedding_dim),
                    str(settings.min_relevance), str(settings.min_cosine), prompt_version, KEY_FORMAT,
                    PROMPT_VERSIONS.get(prompt_version, "")])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class CacheKey:
    tenant_id: int
    scope: str
    question: str
    kb_signature: str
    config: str


def make_key(db: Session, settings: Settings, tenant: Tenant, user: User, question: str,
             prompt_version: str = DEFAULT_PROMPT_VERSION) -> CacheKey:
    signature = ":".join(str(part) for part in index_manager.signature(db, tenant.id))
    scope = "staff" if user.role in ("admin", "agent") else "public"
    return CacheKey(tenant.id, scope, normalize_question(question), signature, config_fingerprint(settings, prompt_version))


def _where(key: CacheKey):
    return (
        AnswerCache.tenant_id == key.tenant_id,
        AnswerCache.scope == key.scope,
        AnswerCache.question == key.question,
        AnswerCache.kb_signature == key.kb_signature,
        AnswerCache.config == key.config,
    )


def lookup(db: Session, key: CacheKey) -> AnswerCache | None:
    now = datetime.now(timezone.utc)
    entry = db.execute(
        select(AnswerCache).where(*_where(key), AnswerCache.expires_at > now).order_by(AnswerCache.id.desc()).limit(1)
    ).scalar_one_or_none()
    if entry is not None and is_refusal(entry.answer):
        return None  # stored before refusals were excluded (N-116): never replayed
    if entry is not None:
        entry.hits += 1
        entry.last_hit_at = now
        db.commit()
    return entry


def store(db: Session, key: CacheKey, answer: str, meta: dict, ttl_hours: float) -> AnswerCache:
    now = datetime.now(timezone.utc)
    entry = AnswerCache(
        tenant_id=key.tenant_id, scope=key.scope, question=key.question, kb_signature=key.kb_signature,
        config=key.config, answer=answer, meta=meta, created_at=now, expires_at=now + timedelta(hours=ttl_hours),
    )
    db.add(entry)
    db.commit()
    return entry
