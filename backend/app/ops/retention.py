"""Data retention: keep personal conversation data only as long as needed.

Old rows are redacted rather than deleted: approvals, refunds and feedback reference runs
and messages (audit and flywheel data must survive), so deleting would either break foreign
keys or lose the audit trail. After ``days``:
- spans of finished runs are deleted (bulk of the trace payload);
- run input/output text and state are replaced by a marker; metrics (tokens, cost,
  latency, verdict) stay for reporting;
- message bodies and metadata are replaced by the marker;
- expired answer-cache entries are deleted.
Runs still waiting for an approval are never touched.

    python -m app.ops.retention --days 90 --dry-run
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..models import AnswerCache, Message, Run, Span

REDACTED = "[已过保留期，内容已清除]"


def apply_retention(db: Session, days: int, dry_run: bool = False) -> dict[str, int]:
    if days < 1:
        raise ValueError("days must be >= 1")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    old_runs = select(Run.id).where(Run.created_at < cutoff, Run.status != "waiting_approval")
    counts = {
        "spans": len(db.execute(select(Span.id).where(Span.run_id.in_(old_runs))).all()),
        "runs": len(db.execute(old_runs.where(Run.input_text != REDACTED)).all()),
        "messages": len(db.execute(select(Message.id).where(Message.created_at < cutoff, Message.content != REDACTED)).all()),
        "answer_cache": len(db.execute(select(AnswerCache.id).where(AnswerCache.expires_at < now)).all()),
    }
    if dry_run:
        return counts
    db.execute(delete(Span).where(Span.run_id.in_(old_runs)))
    db.execute(update(Run).where(Run.id.in_(old_runs)).values(input_text=REDACTED, output_text=REDACTED, state={}),
               execution_options={"synchronize_session": False})
    db.execute(update(Message).where(Message.created_at < cutoff).values(content=REDACTED, meta={}),
               execution_options={"synchronize_session": False})
    db.execute(delete(AnswerCache).where(AnswerCache.expires_at < now))
    db.commit()
    return counts


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wrapper
    from ..config import get_settings
    from ..db import init_engine, new_session

    parser = argparse.ArgumentParser(description="LoopDesk data retention")
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    init_engine(get_settings().database_url)
    with new_session() as db:
        counts = apply_retention(db, args.days, args.dry_run)
    print(("would redact/delete: " if args.dry_run else "redacted/deleted: ") + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
