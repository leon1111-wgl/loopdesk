"""Scan the stored knowledge again with the current rules.

    python -m app.ops.rescan_knowledge

Ingestion scans each chunk once, with the rules of the day. When the rules improve (review round
5, N-038: the old seven regexes let "忽略以前的指令" and phishing text through), the chunks already
stored are not checked again unless something does it, so every instance runs this once when it
starts (main.py); it can also be run by hand.

- A chunk the current rules find something in is held back, with its reasons, for review.
- A chunk an administrator released stays released while the rules find nothing beyond what was
  reviewed; a new reason holds it back again for a new review.
- A chunk the current rules find clean is indexed again.

Changes bump the tenant's knowledge version (cached answers built on a chunk that is now held
back are not served) and take the tenant row first, like every knowledge write. Each change is a
conditional UPDATE on the chunk's held-back flag, so an administrator's release made meanwhile is
not overwritten, and several instances starting together reach the same result.
"""

from __future__ import annotations

import json

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import new_session
from ..models import Chunk, Document, Tenant
from ..rag.ingest import _bump_kb_version, recount_flagged, scan_chunk, tenant_channels
from ..rag.retrieval import index_manager


def _target(reasons: list[str], stored: list[str], reviewed: bool) -> tuple[bool, list[str], bool]:
    """(held back, reasons to store, forget the review) for a chunk the rules now give ``reasons``."""
    if not reasons:
        return False, [], False
    if reviewed and set(reasons) <= set(stored):
        return False, stored, False
    return True, reasons, True


def rescan(db: Session, tenant_id: int | None = None) -> dict[str, int]:
    """All tenants, or one (after its official channels changed)."""
    stats = {"chunks": 0, "held": 0, "released": 0}
    tenants = [tenant_id] if tenant_id is not None else db.execute(select(Tenant.id).order_by(Tenant.id)).scalars().all()
    for tenant_id in tenants:
        channels = tenant_channels(db, tenant_id)
        rows = db.execute(
            select(Chunk.id, Chunk.document_id, Chunk.heading, Chunk.text, Chunk.flagged_injection,
                   Chunk.flag_reasons, Chunk.reviewed_at, Document.visibility)
            .join(Document, Document.id == Chunk.document_id).where(Chunk.tenant_id == tenant_id)
        ).all()
        stats["chunks"] += len(rows)
        changes = []
        for row in rows:
            stored = list(row.flag_reasons or [])
            held, reasons, forget = _target(scan_chunk(row.heading, row.text, row.visibility, channels), stored,
                                            row.reviewed_at is not None)
            if held != row.flagged_injection or reasons != stored:
                changes.append((row, held, reasons, forget))
        if not changes:
            continue
        _bump_kb_version(db, tenant_id)
        touched = set()
        for row, held, reasons, forget in changes:
            values = {"flagged_injection": held, "flag_reasons": reasons}
            if forget:
                values.update(reviewed_by=None, reviewed_at=None)
            done = db.execute(update(Chunk).where(Chunk.id == row.id, Chunk.flagged_injection.is_(row.flagged_injection))
                              .values(**values).execution_options(synchronize_session=False)).rowcount
            if done:
                touched.add(row.document_id)
                if held != row.flagged_injection:
                    stats["held" if held else "released"] += 1
        recount_flagged(db, sorted(touched))
        db.commit()
        index_manager.invalidate(tenant_id)
    return stats


def rescan_knowledge() -> dict[str, int]:
    with new_session() as db:
        return rescan(db)


def main() -> int:  # pragma: no cover - CLI wrapper
    from ..config import get_settings
    from ..db import init_engine

    init_engine(get_settings().database_url)
    print(json.dumps(rescan_knowledge()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
