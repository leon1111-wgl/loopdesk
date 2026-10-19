"""Sign audit entries written before signing existed (before migration 0015).

    python -m app.ops.audit_backfill

Run it once, right after upgrading. Entries are numbered after the tenant's current last
number, oldest first. Do not run it to make a failing verification pass later: an unsigned
entry that appears after the upgrade means someone wrote to the audit table directly.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..audit import _mac
from ..models import AuditLog, Tenant


def sign_unsigned(db: Session) -> int:
    signed = 0
    for entry in db.execute(select(AuditLog).where(AuditLog.seq.is_(None)).order_by(AuditLog.id)).scalars().all():
        entry.seq = db.execute(update(Tenant).where(Tenant.id == entry.tenant_id)
                               .values(audit_seq=Tenant.audit_seq + 1).returning(Tenant.audit_seq)
                               .execution_options(synchronize_session=False)).scalar_one()
        entry.mac = _mac(entry)
        signed += 1
    db.commit()
    return signed


def main() -> int:  # pragma: no cover - CLI wrapper
    from ..config import get_settings
    from ..db import init_engine, new_session

    init_engine(get_settings().database_url)
    with new_session() as db:
        print(f"signed {sign_unsigned(db)} entries")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
