"""Admin view of the answer cache: what is being replayed, how often, and a way to stop it (N-097).

A bad rating already evicts the rows for its question (`agent/answer_cache.evict`); this is for
the answer nobody rated yet. Purging deletes the rows, tombstones included, so the next customer's
question is answered afresh and may be cached again."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..agent.answer_cache import is_evicted
from ..audit import record
from ..db import get_db
from ..models import AnswerCache
from ..security import Principal, require_admin
from .common import get_owned, iso

router = APIRouter(prefix="/api/answer-cache", tags=["answer-cache"])


@router.get("")
def list_entries(principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    """Live rows, most replayed first."""
    rows = db.execute(
        select(AnswerCache).where(AnswerCache.tenant_id == principal.tenant.id,
                                  AnswerCache.expires_at > datetime.now(timezone.utc))
        .order_by(AnswerCache.hits.desc(), AnswerCache.id.desc()).limit(200)
    ).scalars()
    return [{"id": r.id, "question": r.question, "scope": r.scope, "hits": r.hits, "answer_chars": len(r.answer),
             "evicted": (r.meta or {}).get("evicted") if is_evicted(r) else None,
             "source_run_id": (r.meta or {}).get("source_run_id"), "created_at": iso(r.created_at),
             "last_hit_at": iso(r.last_hit_at), "expires_at": iso(r.expires_at)} for r in rows]


@router.delete("/{entry_id}")
def purge_entry(entry_id: int, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    entry = get_owned(db, AnswerCache, entry_id, principal.tenant.id)
    # Every row of that question, so a variant scope or configuration is not left replaying it.
    deleted = _delete(db, AnswerCache.tenant_id == principal.tenant.id, AnswerCache.question == entry.question)
    # The question is a customer's words: the signed audit entry keeps the id and the count only (N-104).
    record(db, principal, "answer_cache.purge", f"answer_cache:{entry_id}", deleted=deleted)
    db.commit()
    return {"deleted": deleted}


@router.delete("")
def purge_all(principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    deleted = _delete(db, AnswerCache.tenant_id == principal.tenant.id)
    record(db, principal, "answer_cache.purge", "answer_cache:*", deleted=deleted)
    db.commit()
    return {"deleted": deleted}


def _delete(db: Session, *where) -> int:
    """Lock in id order first, as `answer_cache.evict` does: a purge and a bad rating on the same
    rows at once must not deadlock on PostgreSQL."""
    ids = list(db.execute(select(AnswerCache.id).where(*where).order_by(AnswerCache.id).with_for_update()).scalars())
    for start in range(0, len(ids), 500):
        db.execute(delete(AnswerCache).where(AnswerCache.id.in_(ids[start:start + 500])))
    return len(ids)
