from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..flywheel.service import FlywheelError, apply_patch, flywheel_stats, reject_patch
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
    return [{"id": p.id, "feedback_id": p.feedback_id, "title": p.title, "content": p.content, "status": p.status,
             "document_id": p.document_id, "created_at": iso(p.created_at), "decided_at": iso(p.decided_at)}
            for p in rows]


@router.post("/patches/{patch_id}/apply")
def apply(patch_id: int, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    patch = get_owned(db, KBPatch, patch_id, principal.tenant.id)
    try:
        document = apply_patch(db, patch, principal.user, get_services().embedder)
    except FlywheelError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    conflicts = open_conflict_counts(db, principal.tenant.id).get(document.id, 0)
    return {"patch_id": patch.id, "status": patch.status, "document_id": document.id,
            "document_version": document.version, "conflicts": conflicts}


@router.post("/patches/{patch_id}/reject")
def reject(patch_id: int, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    patch = get_owned(db, KBPatch, patch_id, principal.tenant.id)
    try:
        reject_patch(db, patch, principal.user)
    except FlywheelError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"patch_id": patch.id, "status": patch.status}


@router.get("/stats")
def stats(principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    return flywheel_stats(db, principal.tenant.id)
