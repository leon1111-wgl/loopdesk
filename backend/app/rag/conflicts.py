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
3. They conflict when their values differ in the same dimension. Durations are one
   dimension: minutes, hours and days are converted (24 小时 = 1 天), working days are not
   (a working day is not 24 hours), so "3 天" against "1-3 个工作日" is flagged for a person.
Detected conflicts are stored for an administrator to resolve. Nothing is blocked at
ingestion (the flywheel blocks a patch that contradicts what the corrected answer cited),
but while a conflict is open, a question whose evidence holds both sides goes to a person
(see ``disputed``): the evidence check passes each of two contradicting sentences on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Chunk, Document, KnowledgeConflict
from ..textutil import split_sentences, token_set

_UNIT = r"个工作日|工作日|小时|分钟|天|元|%|积分|GB|TB"
_CLAIM_RE = re.compile(rf"(\d+(?:\.\d+)?(?:\s*[-~～到至]\s*\d+(?:\.\d+)?)?)\s*({_UNIT})")
_NUM_UNIT_RE = re.compile(rf"\d+(?:\.\d+)?(?:\s*[-~～到至]\s*\d+(?:\.\d+)?)?\s*(?:{_UNIT})?")
MIN_SHARED_TOKENS = 2
MIN_OVERLAP = 0.6
MAX_OWN_TOKENS = 1


_HOURS = {"分钟": 1 / 60, "小时": 1.0, "天": 24.0}  # convertible durations, in hours
FLYWHEEL = "flywheel"  # KnowledgeConflict.origin of a conflict a reviewer published a correction over


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
    return claims_in_text(chunk.text, chunk.id, chunk.document_id)


def claims_in_text(text: str, chunk_id: int = 0, document_id: int = 0) -> list[Claim]:
    claims = []
    for raw in split_sentences(text):
        sentence = raw.lstrip("-*• ").strip()
        values: dict[str, set[str]] = {}
        for number, unit in _CLAIM_RE.findall(sentence):
            unit = "个工作日" if unit == "工作日" else unit
            values.setdefault(unit, set()).add(_normalise_value(number))
        if not values:
            continue
        tokens = token_set(_NUM_UNIT_RE.sub(" ", sentence))
        if len(tokens) >= MIN_SHARED_TOKENS:
            claims.append(Claim(chunk_id, document_id, sentence, values, tokens))
    return claims


def _same_topic(a: Claim, b: Claim) -> bool:
    shared = a.tokens & b.tokens
    shorter, longer = (a, b) if len(a.tokens) <= len(b.tokens) else (b, a)
    return (
        len(shared) >= MIN_SHARED_TOKENS
        and len(shared) / len(shorter.tokens) >= MIN_OVERLAP
        and len(shorter.tokens - longer.tokens) <= MAX_OWN_TOKENS
    )


def _dimension(unit: str) -> str:
    return "时长" if unit in _HOURS or unit == "个工作日" else unit


def _hours(value: str, unit: str) -> str:
    def fmt(x: float) -> str:
        return f"{x:g}"
    return "-".join(fmt(float(part) * _HOURS[unit]) for part in value.split("-")) + "小时"


def comparable_values(claim: Claim) -> dict[str, set[str]]:
    """Values per dimension, in a form that compares across units where that is meaningful."""
    out: dict[str, set[str]] = {}
    for unit, values in claim.values.items():
        for value in values:
            shown = _hours(value, unit) if unit in _HOURS else f"{value}{unit}"
            out.setdefault(_dimension(unit), set()).add(shown)
    return out


def conflicting_dimensions(a: Claim, b: Claim) -> dict[str, dict[str, list[str]]]:
    """Dimensions in which two claims state different values."""
    va, vb = comparable_values(a), comparable_values(b)
    return {dim: {"this": sorted(va[dim]), "other": sorted(vb[dim])}
            for dim in va.keys() & vb.keys() if va[dim] != vb[dim]}


def detect_conflicts(db: Session, document: Document) -> int:
    """Recompute open conflicts between ``document`` and every other document of the tenant.

    A conflict a reviewer published a correction over came from comparing the correction with
    what the corrected answer cited, which wording alone cannot reproduce. It stays open while
    the document still contains its sentence and closes itself when an edit removes it."""
    chunks = db.execute(select(Chunk).where(Chunk.document_id == document.id)).scalars().all()
    seen: set[tuple[str, str]] = set()
    for conflict in db.execute(select(KnowledgeConflict).where(
        KnowledgeConflict.status == "open",
        or_(KnowledgeConflict.document_id == document.id, KnowledgeConflict.other_document_id == document.id),
    )).scalars().all():
        if conflict.origin != FLYWHEEL:
            db.delete(conflict)
            continue
        own = conflict.sentence if conflict.document_id == document.id else conflict.other_sentence
        if any(own in chunk.text for chunk in chunks):
            seen.update({(conflict.sentence, conflict.other_sentence), (conflict.other_sentence, conflict.sentence)})
        else:
            conflict.status, conflict.note = "resolved", "文档已修改，冲突的句子已不存在"
            conflict.resolved_at = datetime.now(timezone.utc)
    mine = [c for chunk in chunks if not chunk.flagged_injection for c in extract_claims(chunk)]
    if not mine:
        db.commit()
        return 0
    others = [c for chunk in db.execute(
        select(Chunk).where(Chunk.tenant_id == document.tenant_id, Chunk.document_id != document.id,
                            Chunk.flagged_injection.is_(False))
    ).scalars() for c in extract_claims(chunk)]
    found = 0
    for a in mine:
        for b in others:
            if (a.sentence, b.sentence) in seen or not _same_topic(a, b):
                continue
            detail = conflicting_dimensions(a, b)
            if not detail:
                continue
            seen.add((a.sentence, b.sentence))
            db.add(KnowledgeConflict(
                tenant_id=document.tenant_id, document_id=a.document_id, chunk_id=a.chunk_id,
                other_document_id=b.document_id, other_chunk_id=b.chunk_id,
                sentence=a.sentence, other_sentence=b.sentence, detail=detail,
            ))
            found += 1
    db.commit()
    return found


def record_published_anyway(db: Session, document: Document, found: list[dict], note: str) -> None:
    """Keep the conflicts a reviewer published a correction over on the open list (``found`` as
    returned by flywheel.service.patch_conflicts): until one side is edited or the conflict is
    marked resolved, answers resting on both sides go to a person."""
    listed = {frozenset((c.sentence, c.other_sentence)) for c in db.execute(select(KnowledgeConflict).where(
        KnowledgeConflict.tenant_id == document.tenant_id, KnowledgeConflict.status == "open")).scalars()}
    chunks = db.execute(select(Chunk).where(Chunk.document_id == document.id)).scalars().all()
    for item in found:
        pair = frozenset((item["patch_sentence"], item["existing_sentence"]))
        if pair in listed:  # detection on ingestion has already listed it
            continue
        listed.add(pair)
        db.add(KnowledgeConflict(
            tenant_id=document.tenant_id, document_id=document.id,
            chunk_id=next((c.id for c in chunks if item["patch_sentence"] in c.text), 0),
            other_document_id=item["document_id"], other_chunk_id=item["chunk_id"],
            sentence=item["patch_sentence"], other_sentence=item["existing_sentence"],
            detail=item["detail"], origin=FLYWHEEL, note=note,
        ))
    db.commit()


def disputed(db: Session, tenant_id: int, texts: list[str]) -> list[KnowledgeConflict]:
    """Open conflicts with both sides among ``texts``, the knowledge one answer would rest on.
    Matched by sentence rather than chunk id, so re-indexing a document does not hide them."""
    if not texts:
        return []
    rows = db.execute(select(KnowledgeConflict).where(
        KnowledgeConflict.tenant_id == tenant_id, KnowledgeConflict.status == "open")).scalars()
    return [c for c in rows
            if any(c.sentence in text for text in texts) and any(c.other_sentence in text for text in texts)]


def open_conflict_counts(db: Session, tenant_id: int) -> dict[int, int]:
    counts: dict[int, int] = {}
    rows = db.execute(select(KnowledgeConflict.document_id, KnowledgeConflict.other_document_id).where(
        KnowledgeConflict.tenant_id == tenant_id, KnowledgeConflict.status == "open")).all()
    for doc_a, doc_b in rows:
        for doc in (doc_a, doc_b):
            counts[doc] = counts.get(doc, 0) + 1
    return counts
