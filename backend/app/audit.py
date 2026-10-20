"""Audit log: who changed a risk limit, a policy document, the regression gate, and what staff did
with customers' money and data (refund decisions, exports, handoff replies, tickets).

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
lowering the counter is what the outside anchor is for: every committed entry's number and MAC
also go to the application log (shipped to a log platform the database's writers cannot
rewrite), and ``check_anchors`` compares those lines with the database.
"""

from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from .config import get_settings
from .models import AuditLog, Tenant
from .observability import request_id_var
from .security import Principal

ACTIONS = {
    "tenant.update": "修改租户配置",
    "user.erase": "抹除个人数据",
    "user.sign_out": "强制下线",
    "kb.document.create": "新增知识文档",
    "kb.document.update": "编辑知识文档",
    "kb.document.delete": "删除知识文档",
    "kb.conflict.resolve": "标记知识冲突已解决",
    "flywheel.patch.apply": "发布知识补丁",
    "flywheel.patch.reject": "驳回知识补丁",
    "eval.case.update": "修改评测用例",
    # Staff work on customers' money and data (review round 3, N-022).
    "approval.approve": "通过审批",
    "approval.reject": "驳回审批",
    "approval.reopen": "审批执行失败，退回待审批",
    "export.download": "导出数据",
    "handoff.reply": "人工接待回复",
    "handoff.close": "结束人工服务",
    "ticket.claim": "接手工单",
    "ticket.close": "处理完成工单",
}
MAX_DIFF_CHARS = 4000
anchor_logger = logging.getLogger("loopdesk.audit")


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
    db.info.setdefault("audit_anchors", []).append(entry)
    return entry


@event.listens_for(Session, "after_commit")
def _log_anchors(session: Session) -> None:
    """Only committed entries are anchored: a rolled-back number is reused by the next entry."""
    for entry in session.info.pop("audit_anchors", []):
        anchor_logger.info("audit anchor", extra={"audit_anchor": {
            "tenant_id": entry.tenant_id, "seq": entry.seq, "mac": entry.mac, "action": entry.action}})


@event.listens_for(Session, "after_rollback")
def _drop_anchors(session: Session) -> None:
    session.info.pop("audit_anchors", None)


def check_anchors(db: Session, anchors: Iterable[dict]) -> dict[str, Any]:
    """Compare anchor lines from the log platform with the database: an anchored entry that is
    gone or carries another MAC, or a tenant counter below its highest anchored number, means
    entries were removed or rewritten, including the newest ones that verify() cannot see."""
    missing, rewritten, behind, checked = [], [], set(), 0
    highest: dict[int, int] = {}
    for anchor in anchors:
        checked += 1
        tenant_id, seq = anchor["tenant_id"], anchor["seq"]
        highest[tenant_id] = max(highest.get(tenant_id, 0), seq)
        stored = db.execute(select(AuditLog.mac).where(AuditLog.tenant_id == tenant_id, AuditLog.seq == seq)).first()
        if stored is None:
            missing.append({"tenant_id": tenant_id, "seq": seq})
        elif not hmac.compare_digest((stored.mac or "").encode(), str(anchor["mac"]).encode()):
            rewritten.append({"tenant_id": tenant_id, "seq": seq})
    for tenant_id, seq in highest.items():
        if (db.execute(select(Tenant.audit_seq).where(Tenant.id == tenant_id)).scalar_one_or_none() or 0) < seq:
            behind.add(tenant_id)
    return {"ok": not missing and not rewritten and not behind, "checked": checked, "missing": missing,
            "rewritten": rewritten, "counter_behind": sorted(behind)}


def verify(db: Session, tenant_id: int) -> dict[str, Any]:
    """Recompute every entry's MAC and check that the numbers run 1..last without gaps."""
    last = db.execute(select(Tenant.audit_seq).where(Tenant.id == tenant_id)).scalar_one()
    entries = db.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.id)).scalars().all()
    signed = [e for e in entries if e.seq is not None and e.mac]
    # Bytes, not str: compare_digest raises on a non-ASCII str, which a forged value can be.
    altered = [e.id for e in signed if not hmac.compare_digest(e.mac.encode(), _mac(e).encode())]
    missing = sorted(set(range(1, last + 1)) - {e.seq for e in signed})
    unsigned = len(entries) - len(signed)
    # An unsigned entry is never fine: anyone with database access can add one. Entries written
    # before signing existed are signed once with python -m app.ops.audit_backfill.
    return {"ok": not altered and not missing and not unsigned, "entries": len(entries), "last_seq": last,
            "altered": altered, "missing": missing, "unsigned": unsigned}


def changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[Any]]:
    """Fields whose value changed, as {field: [old, new]}."""
    return {key: [before.get(key), value] for key, value in after.items() if before.get(key) != value}


def text_diff(old: str, new: str) -> str:
    """The changed lines of a document, so the log shows that "1-3个工作日" became "1-2个工作日"."""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "after", n=1, lineterm="")
    diff = "\n".join(lines)
    return diff if len(diff) <= MAX_DIFF_CHARS else diff[:MAX_DIFF_CHARS] + "\n…（已截断）"
