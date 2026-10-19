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
signals. Matching is exact on a normalised question; semantic (embedding) matching is
deliberately not used, because "退款多久到账" and "退货多久到账" embed close together but
have different answers.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import AnswerCache, Tenant, User
from ..rag.retrieval import index_manager
from .prompts import DEFAULT_PROMPT_VERSION, PROMPT_VERSIONS

_STRIP = re.compile(r"[\s，。！？、；：,.!?;:“”\"'‘’（）()【】\[\]…—\-~～]+")


def normalize_question(text: str) -> str:
    """Fold width/case and drop punctuation and spaces: "退款多久能到账？" == "退款 多久能到账"."""
    return _STRIP.sub("", unicodedata.normalize("NFKC", text).lower())


def config_fingerprint(settings: Settings, prompt_version: str = DEFAULT_PROMPT_VERSION) -> str:
    raw = "|".join([settings.llm_provider, settings.llm_model_strong, settings.llm_model_fast,
                    settings.embedding_provider, str(settings.min_relevance), prompt_version,
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
