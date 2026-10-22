"""The tools one tenant's agent may use: the built-in ones, plus the tools the tenant declared.

The runtime asks ``ctx.tools`` (this view) instead of the process-wide registry, so two tenants on
one server see different tools, a disabled tool disappears from the next turn on, and the agent
registry's tool list (app/governance/agents.py) narrows what the model is offered. Handing over to a
person is always offered, whatever the list says (GB/T 47746-2026: a clear way to a human).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.tools import ToolContext, ToolDef, ToolError, ToolRegistry
from ..models import TenantTool, ToolExecution
from .policy import RUNTIME_RISK, amount_of, amount_text, need_approval
from .schema import SchemaError, validate_arguments

ALWAYS_OFFERED = frozenset({"handoff_to_human"})
ROLES = ("customer", "agent", "admin")


class TenantToolset(ToolRegistry):
    """A registry holding one tenant's tools for one run."""

    def __init__(self, tools: dict[str, ToolDef]):
        super().__init__()
        self._tools = tools


def toolset_for(db: Session, tenant: Any, base: ToolRegistry, allowed: list[str] | None = None,
                include_disabled: bool = False) -> TenantToolset:
    """``include_disabled``: settling what an interrupted approval did (ops.stale_runs) must find a tool
    an administrator switched off since; the agent is never offered one."""
    tools = {t.name: t for t in base.all()}
    query = select(TenantTool).where(TenantTool.tenant_id == tenant.id)
    if not include_disabled:
        query = query.where(TenantTool.enabled.is_(True), TenantTool.status == "active")
    rows = db.execute(query.order_by(TenantTool.id)).scalars().all()
    for row in rows:
        if row.name not in tools:  # a built-in name is refused when the tool is saved; never shadowed
            tools[row.name] = tool_def(row)
    if allowed is not None:
        tools = {name: t for name, t in tools.items() if name in allowed or name in ALWAYS_OFFERED}
    return TenantToolset(tools)


def tool_def(row: TenantTool) -> ToolDef:
    label = row.label or row.name
    approval = dict(row.approval or {})
    requires = list((row.hints or {}).get("requires") or [])
    description = row.description.strip()
    if requires:
        description += f" 调用前必须先调用 {'、'.join(requires)} 核对，参数与核对结果一致。"
    if row.risk != "read" and approval.get("mode", "never") != "never":
        description += " 需要人工审批时会自动进入审批，审批完成前不要向顾客承诺结果。"

    def precheck(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        try:
            checked = validate_arguments(row.parameters or {}, args)
        except SchemaError as exc:
            raise ToolError(str(exc)) from None
        if row.kind == "http":
            from .http_tool import check_path_values

            check_path_values(row.binding or {}, checked)
        for name in requires:
            _require_prior(ctx, name, checked)
        return checked

    def handler(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        if row.kind == "mcp":
            from .mcp_client import call_mcp_tool

            return call_mcp_tool(ctx, row, label, args)
        from .http_tool import HttpToolCaller

        return HttpToolCaller(ctx, row, label)(args)

    def policy(ctx: ToolContext, args: dict[str, Any]):
        return None if row.risk == "read" else need_approval(ctx, label, approval, args)

    def executed(db: Session, approval_id: int) -> dict[str, Any] | None:
        """What an approval's call did, as the ledger knows it (ops.stale_runs asks after a crash)."""
        from ..models import Approval
        from .http_tool import check_ticket, unknown_message

        done = db.execute(select(ToolExecution).where(
            ToolExecution.approval_id == approval_id, ToolExecution.tool_name == row.name)).scalars().first()
        if done is None or done.status == "not_sent":
            return None  # nothing left: the approval can be decided again
        if done.status == "succeeded":
            return dict(done.result)
        if done.status == "failed":  # refused: nothing to verify, the customer hears why
            return {"status": "failed", "message": f"{label}未能办理：{done.error or '业务系统没有受理'}。"}
        approval = db.get(Approval, approval_id)
        check_ticket(db, done, label, done.error or "处理被中断", customer_id=approval.requested_by)
        db.flush()
        return {"status": done.status, "message": unknown_message(label, done.ticket_id)}

    def money(args: dict[str, Any], tenant_currency: str) -> str:
        amount, currency = amount_of(approval, args, tenant_currency)
        return amount_text(amount, currency)

    return ToolDef(
        name=row.name, description=description, parameters=row.parameters or {"type": "object", "properties": {}},
        handler=handler, risk=RUNTIME_RISK.get(row.risk, "write"), roles=tuple(row.roles or ROLES),
        precheck=precheck, approval=policy if row.risk != "read" else None,
        executed=executed if row.risk != "read" else None,
        source=row.kind, label=label, amount_text=money if approval.get("amount_field") else None,
        hints={**(row.hints or {}), "custom": True, "risk": row.risk, "label": label,
               "returns_pii": bool((row.flags or {}).get("returns_pii"))},
    )


def _require_prior(ctx: ToolContext, name: str, args: dict[str, Any]) -> None:
    """A write that needs a read first ("look the booking up before refunding it"): the read must have
    succeeded in this run, for the same values of the parameters both share."""
    for item in reversed(ctx.evidence):
        if item.kind != "tool" or item.title != name:
            continue
        try:
            output = json.loads(item.text)
        except ValueError:
            continue
        if not isinstance(output, dict) or output.get("error"):
            continue
        if all(str(output.get(k)) == str(v) for k, v in args.items() if k in output and isinstance(v, (str, int))):
            return
    raise ToolError(f"请先调用 {name} 核对后再办理")
