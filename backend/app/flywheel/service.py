"""Data flywheel: turn one human correction into three durable assets.

    correction ─┬─► failure attribution (why did the agent get it wrong?)
                ├─► eval case candidate (so the same mistake is caught by CI next time)
                └─► knowledge patch draft (so the agent can answer correctly next time)

A human approves the patch; applying it re-indexes the knowledge base and activates the
eval case. Running the suite afterwards shows the case flip from fail to pass without
breaking the others: that is the loop closing.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..agent.prompts import KB_PATCH_PROMPT
from ..llm.base import Embedder, LLMMessage, LLMProvider
from ..models import Chunk, Document, EvalCase, EvalRun, Feedback, KBPatch, Message, Run, Span, Tenant, User
from ..rag.conflicts import claims_in_text, conflicting_dimensions, extract_claims, record_published_anyway
from ..rag.ingest import ingest_document

REASONS = {
    "knowledge_gap": "知识缺失",
    "knowledge_outdated": "知识过期",
    "retrieval_miss": "检索未命中",
    "hallucination": "答案无依据",
    "tool_error": "工具或流程错误",
    "tone": "语气或表达",
    "other": "其他",
}
PATCH_DOC_SOURCE = "flywheel_patches.md"
PATCH_DOC_HEADER = "# 客服纠错知识补丁\n\n> 由数据飞轮从客服纠错中生成，经管理员审核后发布。\n"
_FACT_RE = re.compile(r"\d+(?:\.\d+)?(?:%|元|天|小时|个工作日|积分|GB|TB)?")


class FlywheelError(ValueError):
    pass


def attribute_failure(db: Session, run: Run | None) -> str:
    """Best-effort root cause from the trace. Humans can override it in the feedback form."""
    if run is None:
        return "other"
    spans = db.execute(select(Span).where(Span.run_id == run.id)).scalars().all()
    if any(s.kind == "tool" and s.status == "error" for s in spans):
        return "tool_error"
    if run.verdict in ("blocked", "warn"):
        return "hallucination"
    searches = [s for s in spans if s.kind == "tool" and s.name == "search_knowledge"]
    if searches:
        hits = [h for s in searches for h in (s.output.get("output", {}).get("results") or [])]
        if not hits:
            return "knowledge_gap"
        if max(float(h.get("relevance", 0)) for h in hits) < 0.5:
            return "retrieval_miss"
    return "other"


def key_facts(correction: str) -> list[str]:
    # A masked number or address is not a fact to look for (only its own characters: no spaces in Chinese).
    correction = re.sub(r"[0-9A-Za-z@.+*-]*\*{3,}[0-9A-Za-z@.+*-]*", " ", correction)
    facts = [m.group(0) for m in _FACT_RE.finditer(correction) if len(m.group(0)) >= 2 or m.group(0).isdigit()]
    unique = list(dict.fromkeys(facts))
    if unique:
        return unique[:3]
    clause = re.split(r"[，。；,;！!？?]", correction.strip())[0]
    return [clause[:12]] if clause else []


def submit_feedback(
    db: Session,
    *,
    user: User,
    message: Message,
    rating: str,
    correction: str = "",
    reason_category: str = "",
    comment: str = "",
    provider: LLMProvider,
    model: str,
) -> dict:
    if message.role != "assistant":
        raise FlywheelError("只能对助手回复进行反馈")
    if rating not in ("up", "down", "correction"):
        raise FlywheelError("rating 必须是 up / down / correction")
    if rating == "correction":
        if user.role not in ("admin", "agent"):
            raise FlywheelError("只有客服或管理员可以提交纠错")
        if not correction.strip():
            raise FlywheelError("请填写正确答案")
    if reason_category and reason_category not in REASONS:
        raise FlywheelError("未知的原因分类")

    question = db.execute(
        select(Message.content)
        .where(Message.conversation_id == message.conversation_id, Message.role == "user", Message.id < message.id)
        .order_by(Message.id.desc())
        .limit(1)
    ).scalar_one_or_none() or ""
    run = db.get(Run, message.run_id) if message.run_id else None
    attribution = attribute_failure(db, run) if rating != "up" else ""

    # One rating per user and message, the latest click wins; a correction is identified by its
    # text, so the same one sent twice returns the first result instead of a second eval case
    # and a second patch. Feedback counts on the dashboard are then people, not clicks.
    basis = f"{message.id}:{user.id}:" + (f"correction:{correction.strip()}" if rating == "correction" else "rating")
    dedup_key = hashlib.sha256(basis.encode()).hexdigest()
    feedback = Feedback(
        tenant_id=message.tenant_id, message_id=message.id, run_id=message.run_id, user_id=user.id,
        rating=rating, question=question, answer=message.content, correction=correction.strip(),
        reason_category=reason_category or (attribution if rating != "up" else ""),
        attribution=attribution, comment=comment, dedup_key=dedup_key,
    )
    existing = db.execute(select(Feedback).where(Feedback.dedup_key == dedup_key)).scalar_one_or_none()
    if existing is not None:
        return _repeated(db, existing, feedback)

    case = patch = None
    if rating == "correction":
        asker = db.get(User, run.user_id) if run else None
        used_search = bool(run) and db.execute(
            select(func.count(Span.id)).where(Span.run_id == run.id, Span.name == "search_knowledge")
        ).scalar_one() > 0
        case = EvalCase(
            tenant_id=message.tenant_id, suite="default", question=question,
            role=asker.role if asker else "customer", as_user=asker.email if asker else None,
            expected_tools=["search_knowledge"] if used_search else [], must_include=key_facts(correction),
            expect_refusal=False, tags=["flywheel", feedback.reason_category], source="flywheel", status="candidate",
        )
        if feedback.reason_category in ("knowledge_gap", "knowledge_outdated", "retrieval_miss"):
            # Drafted before anything is written: if the model call fails, nothing is left
            # behind, and the same correction sent again starts over (a committed feedback row
            # would have made the retry look like a duplicate with no case and no patch).
            payload = json.dumps({"question": question, "correction": correction.strip()}, ensure_ascii=False)
            draft = provider.chat(
                [LLMMessage(role="user", content=KB_PATCH_PROMPT.format(payload=payload))], [], model,
                {"task": "kb_patch", "temperature": 0},
            ).content.strip() or f"## {question}\n\n{correction.strip()}"
            patch = KBPatch(tenant_id=message.tenant_id, title=f"纠错补丁：{question[:40]}", content=draft.strip())

    # Feedback, eval case and patch are written in one transaction.
    db.add(feedback)
    try:
        db.flush()
        if case is not None:
            case.key, case.feedback_id = f"flywheel_{feedback.id}", feedback.id
            db.add(case)
        if patch is not None:
            patch.feedback_id = feedback.id
            db.add(patch)
        db.commit()
    except IntegrityError:  # the same feedback arrived twice at once: the first one stands
        db.rollback()
        first = db.execute(select(Feedback).where(Feedback.dedup_key == dedup_key)).scalar_one()
        return _repeated(db, first, feedback)
    return _feedback_result(db, feedback)


def _repeated(db: Session, existing: Feedback, new: Feedback) -> dict:
    """A rating replaces the user's earlier rating; a repeated correction returns the first one."""
    if new.rating != "correction":
        existing.rating, existing.comment = new.rating, new.comment
        existing.reason_category, existing.attribution = new.reason_category, new.attribution
        db.commit()
    return _feedback_result(db, existing)


def _feedback_result(db: Session, feedback: Feedback) -> dict:
    case_id = db.execute(select(EvalCase.id).where(EvalCase.feedback_id == feedback.id)).scalar_one_or_none()
    patch_id = db.execute(select(KBPatch.id).where(KBPatch.feedback_id == feedback.id)).scalar_one_or_none()
    return {"feedback_id": feedback.id, "attribution": feedback.attribution, "eval_case_id": case_id,
            "patch_id": patch_id}


class PatchConflict(FlywheelError):
    """The correction states numbers that disagree with the knowledge the corrected answer cited."""

    def __init__(self, conflicts: list[dict]):
        super().__init__("补丁与被纠正回答所引用的知识存在数字冲突")
        self.conflicts = conflicts


def patch_conflicts(db: Session, patch: KBPatch) -> list[dict]:
    """Numbers in the correction that disagree with what the corrected answer cited.

    The corrected answer's citations name exactly the knowledge the correction disputes, so
    this is precise where a tenant-wide topic match would not be. Either the agent was wrong
    and the source document is still right (reject the patch), or the knowledge is outdated
    and the source document has to be changed too (a patch alone would leave both versions
    in the knowledge base, retrievable side by side)."""
    feedback = db.get(Feedback, patch.feedback_id)
    message = db.get(Message, feedback.message_id) if feedback else None
    chunk_ids = {(c.get("meta") or {}).get("chunk_id") for c in ((message.meta or {}).get("citations") or [])} if message else set()
    cited = []
    for chunk_id in sorted(i for i in chunk_ids if i):
        chunk = db.get(Chunk, chunk_id)
        if chunk is not None and chunk.tenant_id == patch.tenant_id:
            title = db.get(Document, chunk.document_id).title
            cited.extend((title, chunk, claim) for claim in extract_claims(chunk))
    found = []
    for a in claims_in_text(patch.content):
        # Compare with the cited claims this one overlaps most: "微信…1-2个工作日" is about the
        # 微信 line, not the 银行卡 line; a generic "3天内到账" ties with both and disputes both.
        overlap = {id(b): len(a.tokens & b.tokens) for _, _, b in cited}
        best = max(overlap.values(), default=0)
        if best < 2:
            continue
        for title, chunk, b in cited:
            detail = conflicting_dimensions(a, b) if overlap[id(b)] == best else {}
            if detail:
                found.append({"patch_sentence": a.sentence, "document": title, "existing_sentence": b.sentence,
                              "detail": detail, "document_id": chunk.document_id, "chunk_id": chunk.id})
    return found


def _claim(db: Session, patch: KBPatch, status: str, reviewer: User) -> None:
    """Move a proposed patch to its decision in one conditional UPDATE: of two reviewers
    acting on the same patch at once, exactly one wins."""
    claimed = db.execute(
        update(KBPatch).where(KBPatch.id == patch.id, KBPatch.status == "proposed")
        .values(status=status, reviewer_id=reviewer.id, decided_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session="fetch")
    ).rowcount
    if claimed != 1:
        db.rollback()
        raise FlywheelError("该补丁已处理")


def apply_patch(db: Session, patch: KBPatch, reviewer: User, embedder: Embedder,
                acknowledge_conflicts: bool = False) -> tuple[Document, list[dict]]:
    """Publish a patch. A patch that contradicts the knowledge the corrected answer cited needs
    an explicit acknowledgement, and even then its eval case is not activated: a wrong
    correction must not certify itself by becoming a regression test."""
    conflicts = patch_conflicts(db, patch)
    if conflicts and not acknowledge_conflicts:
        raise PatchConflict(conflicts)
    # Start from the latest committed state (the caller's session has already read), and
    # write first: on SQLite the claim takes the write lock, so concurrent publishers take
    # turns; on PostgreSQL the tenant row lock below does the same. Publishing is rare, so
    # serialising it per tenant is cheap. Without this, two publishers read the same patch
    # document and the second write dropped the first patch, or, on the very first publish,
    # both created a patch document and every later publish failed.
    db.commit()
    _claim(db, patch, "applied", reviewer)
    # FOR NO KEY UPDATE: serialises with other edits and publishes, while inserts that reference
    # the tenant (a chat's runs, messages, spans) still go through.
    db.execute(select(Tenant.id).where(Tenant.id == patch.tenant_id).with_for_update(key_share=True))
    document = db.execute(
        select(Document).where(Document.tenant_id == patch.tenant_id, Document.source == PATCH_DOC_SOURCE)
    ).scalar_one_or_none()
    base = document.content if document else PATCH_DOC_HEADER
    content = f"{base.rstrip()}\n\n{patch.content.strip()}\n"
    document = ingest_document(
        db, tenant_id=patch.tenant_id, title="客服纠错知识补丁", content=content, embedder=embedder,
        visibility="public", source=PATCH_DOC_SOURCE, created_by=reviewer.id, document=document,
    )
    patch.document_id = document.id
    if conflicts:
        record_published_anyway(db, document, conflicts, f"补丁 #{patch.id} 经审核人确认后发布")
    else:
        for case in db.execute(select(EvalCase).where(EvalCase.feedback_id == patch.feedback_id)).scalars():
            if case.status == "candidate":
                case.status = "active"
    db.commit()
    return document, conflicts


def reject_patch(db: Session, patch: KBPatch, reviewer: User) -> None:
    db.commit()
    _claim(db, patch, "rejected", reviewer)
    db.commit()


def flywheel_stats(db: Session, tenant_id: int) -> dict:
    feedback = db.execute(select(Feedback).where(Feedback.tenant_id == tenant_id)).scalars().all()
    patches = db.execute(select(KBPatch.status).where(KBPatch.tenant_id == tenant_id)).scalars().all()
    cases = db.execute(select(EvalCase.source, EvalCase.status).where(EvalCase.tenant_id == tenant_id)).all()
    last_runs = db.execute(
        select(EvalRun).where(EvalRun.tenant_id == tenant_id, EvalRun.status == "finished")
        .order_by(EvalRun.id.desc()).limit(10)
    ).scalars().all()
    return {
        "feedback": dict(Counter(f.rating for f in feedback)),
        "attribution": dict(Counter(f.reason_category or f.attribution for f in feedback if f.rating != "up")),
        "reason_labels": REASONS,
        "patches": dict(Counter(patches)),
        "eval_cases": {
            "total": len(cases),
            "from_flywheel": sum(1 for s, _ in cases if s == "flywheel"),
            "candidates": sum(1 for _, st in cases if st == "candidate"),
        },
        "pass_rate_history": [
            {"id": r.id, "pass_rate": r.metrics.get("pass_rate"), "total": r.total, "created_at": r.created_at.isoformat()}
            for r in reversed(last_runs)
        ],
    }
