"""Knowledge conflict detection at ingestion time.

When two documents state the same rule with different numbers ("退款 1-3 个工作日到账" in
the policy, "1-2 个工作日" in a later patch), retrieval may surface either one and the
model has no way to know which is current. Conflicts are therefore caught when a document
is written, not when a customer gets the wrong answer.

Heuristic, deliberately conservative:
1. Split both documents into sentences and keep the ones that contain a number with a
   unit (天、个工作日、小时、元、%、积分 …): these are the checkable claims.
2. Two claims from different documents are "about the same thing" when they share at
   least 2 content tokens, the overlap covers ≥ 60% of the shorter claim, and the
   shorter claim has at most one token of its own (numbers and units are removed first).
   The last rule keeps "微信、支付宝 1-3 天" and "银行卡 3-7 天" apart: same wording,
   different subject.
3. They conflict when they use the same unit but different values.
Detected conflicts are stored for an administrator to resolve; nothing is blocked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ..models import Chunk, Document, KnowledgeConflict
from ..textutil import split_sentences, token_set

_UNIT = r"个工作日|工作日|小时|分钟|天|元|%|积分|GB|TB"
_CLAIM_RE = re.compile(rf"(\d+(?:\.\d+)?(?:\s*[-~～到至]\s*\d+(?:\.\d+)?)?)\s*({_UNIT})")
_NUM_UNIT_RE = re.compile(rf"\d+(?:\.\d+)?(?:\s*[-~～到至]\s*\d+(?:\.\d+)?)?\s*(?:{_UNIT})?")
MIN_SHARED_TOKENS = 2
MIN_OVERLAP = 0.6
MAX_OWN_TOKENS = 1


@dataclass
class Claim:
    chunk_id: int
    document_id: int
    sentence: str
    values: dict[str, set[str]]  # unit -> normalised values
    tokens: set[str]


def _normalise_value(raw: str) -> str:
    return re.sub(r"\s*[~～到至]\s*", "-", raw.replace(" ", ""))


def extract_claims(chunk: Chunk) -> list[Claim]:
    claims = []
    for raw in split_sentences(chunk.text):
        sentence = raw.lstrip("-*• ").strip()
        values: dict[str, set[str]] = {}
        for number, unit in _CLAIM_RE.findall(sentence):
            unit = "个工作日" if unit == "工作日" else unit
            values.setdefault(unit, set()).add(_normalise_value(number))
        if not values:
            continue
        tokens = token_set(_NUM_UNIT_RE.sub(" ", sentence))
        if len(tokens) >= MIN_SHARED_TOKENS:
            claims.append(Claim(chunk.id, chunk.document_id, sentence, values, tokens))
    return claims


def _same_topic(a: Claim, b: Claim) -> bool:
    shared = a.tokens & b.tokens
    shorter, longer = (a, b) if len(a.tokens) <= len(b.tokens) else (b, a)
    return (
        len(shared) >= MIN_SHARED_TOKENS
        and len(shared) / len(shorter.tokens) >= MIN_OVERLAP
        and len(shorter.tokens - longer.tokens) <= MAX_OWN_TOKENS
    )


def _conflicting_units(a: Claim, b: Claim) -> list[str]:
    return [unit for unit in a.values.keys() & b.values.keys() if a.values[unit] != b.values[unit]]


def detect_conflicts(db: Session, document: Document) -> int:
    """Recompute open conflicts between ``document`` and every other document of the tenant."""
    db.execute(delete(KnowledgeConflict).where(
        KnowledgeConflict.status == "open",
        or_(KnowledgeConflict.document_id == document.id, KnowledgeConflict.other_document_id == document.id),
    ))
    mine = [c for chunk in db.execute(select(Chunk).where(Chunk.document_id == document.id)).scalars()
            if not chunk.flagged_injection for c in extract_claims(chunk)]
    if not mine:
        db.commit()
        return 0
    others = [c for chunk in db.execute(
        select(Chunk).where(Chunk.tenant_id == document.tenant_id, Chunk.document_id != document.id,
                            Chunk.flagged_injection.is_(False))
    ).scalars() for c in extract_claims(chunk)]
    found = 0
    seen: set[tuple[str, str]] = set()
    for a in mine:
        for b in others:
            if (a.sentence, b.sentence) in seen or not _same_topic(a, b):
                continue
            units = _conflicting_units(a, b)
            if not units:
                continue
            seen.add((a.sentence, b.sentence))
            db.add(KnowledgeConflict(
                tenant_id=document.tenant_id, document_id=a.document_id, chunk_id=a.chunk_id,
                other_document_id=b.document_id, other_chunk_id=b.chunk_id,
                sentence=a.sentence, other_sentence=b.sentence,
                detail={unit: {"this": sorted(a.values[unit]), "other": sorted(b.values[unit])} for unit in units},
            ))
            found += 1
    db.commit()
    return found


def open_conflict_counts(db: Session, tenant_id: int) -> dict[int, int]:
    counts: dict[int, int] = {}
    rows = db.execute(select(KnowledgeConflict.document_id, KnowledgeConflict.other_document_id).where(
        KnowledgeConflict.tenant_id == tenant_id, KnowledgeConflict.status == "open")).all()
    for doc_a, doc_b in rows:
        for doc in (doc_a, doc_b):
            counts[doc] = counts.get(doc, 0) + 1
    return counts
