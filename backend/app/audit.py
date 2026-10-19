"""Administrator audit log: who changed a risk limit, a policy document, the regression gate.

Entries are added to the caller's transaction. An operation that commits once (tenant
settings, resolving a conflict, editing an eval case) therefore records its entry
atomically with the change; operations that commit in several steps (re-indexing a
document, publishing a patch) record it right after they succeed.

Tamper evidence: each entry gets the tenant's next number (one atomic UPDATE ... RETURNING
on the tenant row, so numbers have no gaps and no duplicates) and an HMAC over its content
and number, keyed with LOOPDESK_AUDIT_KEY, which is not stored in the database. Someone who
can write to the database but does not hold the key cannot change an entry or delete one from
the middle without ``verify`` noticing. A plain hash chain in the same database would not
give that: whoever can edit the rows can recompute the chain. Cutting entries off the end and
lowering the counter stays undetectable without an outside anchor (export the latest number
and MAC to a log system regularly).
"""

from __future__ import annotations

import difflib
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import get_settings
from .models import AuditLog, Tenant
from .observability import request_id_var
from .security import Principal

ACTIONS = {
    "tenant.update": "修改租户配置",
    "user.erase": "抹除个人数据",
    "kb.document.create": "新增知识文档",
    "kb.document.update": "编辑知识文档",
    "kb.document.delete": "删除知识文档",
    "kb.conflict.resolve": "标记知识冲突已解决",
    "flywheel.patch.apply": "发布知识补丁",
    "flywheel.patch.reject": "驳回知识补丁",
    "eval.case.update": "修改评测用例",
}
MAX_DIFF_CHARS = 4000


def _key() -> bytes:
    settings = get_settings()
    if settings.audit_key:
        return settings.audit_key.encode()
    return hashlib.sha256(b"loopdesk-audit:" + settings.jwt_secret.encode()).digest()


def _mac(entry: AuditLog) -> str:
    created = entry.created_at if entry.created_at.tzinfo else entry.created_at.replace(tzinfo=timezone.utc)
    content = json.dumps({
        "tenant_id": entry.tenant_id, "seq": entry.seq, "actor_id": entry.actor_id, "action": entry.action,
        "target": entry.target, "detail": entry.detail, "request_id": entry.request_id,
        "created_at": created.astimezone(timezone.utc).isoformat(),
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hmac.new(_key(), content.encode(), hashlib.sha256).hexdigest()


def record(db: Session, principal: Principal, action: str, target: str = "", **detail: Any) -> AuditLog:
    assert action in ACTIONS, action
    seq = db.execute(update(Tenant).where(Tenant.id == principal.tenant.id).values(audit_seq=Tenant.audit_seq + 1)
                     .returning(Tenant.audit_seq).execution_options(synchronize_session=False)).scalar_one()
    entry = AuditLog(tenant_id=principal.tenant.id, actor_id=principal.user.id, action=action, target=target,
                     detail=detail, request_id=request_id_var.get(), created_at=datetime.now(timezone.utc), seq=seq)
    entry.mac = _mac(entry)
    db.add(entry)
    return entry


def verify(db: Session, tenant_id: int) -> dict[str, Any]:
    """Recompute every entry's MAC and check that the numbers run 1..last without gaps."""
    last = db.execute(select(Tenant.audit_seq).where(Tenant.id == tenant_id)).scalar_one()
    entries = db.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.id)).scalars().all()
    signed = [e for e in entries if e.seq is not None]
    altered = [e.id for e in signed if not hmac.compare_digest(e.mac or "", _mac(e))]
    missing = sorted(set(range(1, last + 1)) - {e.seq for e in signed})
    return {"ok": not altered and not missing, "entries": len(entries), "last_seq": last,
            "altered": altered, "missing": missing, "unsigned": len(entries) - len(signed)}


def changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[Any]]:
    """Fields whose value changed, as {field: [old, new]}."""
    return {key: [before.get(key), value] for key, value in after.items() if before.get(key) != value}


def text_diff(old: str, new: str) -> str:
    """The changed lines of a document, so the log shows that "1-3个工作日" became "1-2个工作日"."""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "after", n=1, lineterm="")
    diff = "\n".join(lines)
    return diff if len(diff) <= MAX_DIFF_CHARS else diff[:MAX_DIFF_CHARS] + "\n…（已截断）"
