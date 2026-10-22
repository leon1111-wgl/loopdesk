from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import record
from ..db import get_db
from ..flywheel.service import FlywheelError, PatchConflict, apply_patch, flywheel_stats, reject_patch
from ..rag.conflicts import open_conflict_counts
from ..models import EvalCase, Feedback, KBPatch, User
from ..security import Principal, require_admin, require_staff
from ..services import get_services
from .common import get_owned, iso

router = APIRouter(prefix="/api/flywheel", tags=["flywheel"])


@router.get("/feedback")
def list_feedback(principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        select(Feedback, User).join(User, User.id == Feedback.user_id)
        .where(Feedback.tenant_id == principal.tenant.id).order_by(Feedback.id.desc()).limit(200)
    ).all()
    patches = {p.feedback_id: p for p in db.execute(
        select(KBPatch).where(KBPatch.tenant_id == principal.tenant.id)).scalars()}
    cases = {c.feedback_id: c for c in db.execute(
        select(EvalCase).where(EvalCase.tenant_id == principal.tenant.id, EvalCase.feedback_id.is_not(None))).scalars()}
    out = []
    for f, u in rows:
        patch, case = patches.get(f.id), cases.get(f.id)
        out.append({
            "id": f.id, "rating": f.rating, "question": f.question, "answer": f.answer, "correction": f.correction,
            "reason_category": f.reason_category, "attribution": f.attribution, "comment": f.comment,
            "run_id": f.run_id, "message_id": f.message_id, "user": {"name": u.name, "role": u.role},
            "created_at": iso(f.created_at),
            "patch": {"id": patch.id, "status": patch.status} if patch else None,
            "eval_case": {"id": case.id, "key": case.key, "status": case.status} if case else None,
        })
    return out


@router.get("/patches")
def list_patches(principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        select(KBPatch).where(KBPatch.tenant_id == principal.tenant.id).order_by(KBPatch.id.desc())
    ).scalars()
    return [{"id": p.id, "kind": p.kind, "feedback_id": p.feedback_id, "title": p.title, "content": p.content,
             "payload": p.payload, "status": p.status, "document_id": p.document_id, "created_at": iso(p.created_at),
             "decided_at": iso(p.decided_at)}
            for p in rows]


class ApplyIn(BaseModel):
    acknowledge_conflicts: bool = False  # the reviewer has seen the conflicts and publishes anyway
    skip_questions: list[str] = Field(default_factory=list, max_length=500)  # questions patch: what the reviewer dropped


@router.post("/patches/{patch_id}/apply")
def apply(patch_id: int, body: ApplyIn | None = None, principal: Principal = Depends(require_admin),
          db: Session = Depends(get_db)):
    patch = get_owned(db, KBPatch, patch_id, principal.tenant.id)
    try:
        document, cited = apply_patch(db, patch, principal.user, get_services().embedder,
                                      acknowledge_conflicts=bool(body and body.acknowledge_conflicts),
                                      skip_questions=body.skip_questions if body else ())
    except PatchConflict as exc:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={
            "detail": "补丁与被纠正回答所引用的知识存在数字冲突。如果是坐席纠错有误，请驳回；如果知识已经过期，"
                      "请先在「知识库」中编辑原文档，再确认发布。", "conflicts": exc.conflicts})
    except FlywheelError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if patch.kind == "questions":
        published = (patch.payload or {}).get("published") or {}
        record(db, principal, "flywheel.patch.apply", f"patch:{patch.id}", **_content_digest(patch),
               kind="questions", document_id=document.id, questions_indexed=published.get("indexed", 0),
               questions_skipped=len(published.get("skipped") or []), questions_stale=published.get("stale", 0))
        db.commit()
        return {"patch_id": patch.id, "status": patch.status, "document_id": document.id,
                "questions_indexed": published.get("indexed", 0), "questions_stale": published.get("stale", 0)}
    record(db, principal, "flywheel.patch.apply", f"patch:{patch.id}", **_content_digest(patch),
           document_version=document.version, acknowledged_conflicts=len(cited))
    db.commit()
    conflicts = open_conflict_counts(db, principal.tenant.id).get(document.id, 0)
    return {"patch_id": patch.id, "status": patch.status, "document_id": document.id,
            "document_version": document.version, "conflicts": conflicts,
            "cited_conflicts": len(cited), "eval_case_activated": not cited}


def _content_digest(patch: KBPatch) -> dict:
    """What the audit log keeps of a patch: a draft quotes the customer's question, and a signed audit
    entry cannot be erased later with the rest of their data (N-104). The digest still proves which
    text was decided on (compare it with the patch or the document version)."""
    return {"feedback_id": patch.feedback_id, "content_chars": len(patch.content),
            "content_sha256": hashlib.sha256(patch.content.encode()).hexdigest()}


@router.post("/patches/{patch_id}/reject")
def reject(patch_id: int, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    patch = get_owned(db, KBPatch, patch_id, principal.tenant.id)
    try:
        reject_patch(db, patch, principal.user)
    except FlywheelError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    record(db, principal, "flywheel.patch.reject", f"patch:{patch.id}", **_content_digest(patch))
    db.commit()
    return {"patch_id": patch.id, "status": patch.status}


@router.get("/stats")
def stats(principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    return flywheel_stats(db, principal.tenant.id)
