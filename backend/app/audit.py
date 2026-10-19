"""Administrator audit log: who changed a risk limit, a policy document, the regression gate.

Entries are added to the caller's transaction. An operation that commits once (tenant
settings, resolving a conflict, editing an eval case) therefore records its entry
atomically with the change; operations that commit in several steps (re-indexing a
document, publishing a patch) record it right after they succeed.
"""

from __future__ import annotations

import difflib
from typing import Any

from sqlalchemy.orm import Session

from .models import AuditLog
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


def record(db: Session, principal: Principal, action: str, target: str = "", **detail: Any) -> AuditLog:
    assert action in ACTIONS, action
    entry = AuditLog(tenant_id=principal.tenant.id, actor_id=principal.user.id, action=action,
                     target=target, detail=detail, request_id=request_id_var.get())
    db.add(entry)
    return entry


def changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[Any]]:
    """Fields whose value changed, as {field: [old, new]}."""
    return {key: [before.get(key), value] for key, value in after.items() if before.get(key) != value}


def text_diff(old: str, new: str) -> str:
    """The changed lines of a document, so the log shows that "1-3个工作日" became "1-2个工作日"."""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "after", n=1, lineterm="")
    diff = "\n".join(lines)
    return diff if len(diff) <= MAX_DIFF_CHARS else diff[:MAX_DIFF_CHARS] + "\n…（已截断）"
