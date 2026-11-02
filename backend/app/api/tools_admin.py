"""Tenant tools: the administrator declares them, the agent uses them (app/tooling).

Every change is checked as a whole (tooling/definitions.py), bumps the tool's version and is
audited with what changed. Secrets are references; their values are never stored or returned.
The egress allowlist is shown, not edited: the platform operator sets it in the tenant pack.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..audit import changes, record
from ..db import get_db
from ..models import Approval, McpServer, Run, Span, TenantTool, ToolExecution
from ..security import Principal, require_admin, require_staff
from ..services import get_services
from ..tooling.definitions import DefinitionError, check_definition, residency
from ..tooling.egress import EgressError, EgressPolicy
from ..tooling.mcp_client import McpError, sync_server
from ..tooling import secrets
from .common import get_owned, iso

router = APIRouter(prefix="/api/admin", tags=["tools"])

BUILTIN_RISK = {"read": "read", "write": "write", "high": "money"}


class ToolIn(BaseModel):
    name: str = Field(max_length=48)
    label: str = Field(max_length=64)
    description: str = Field(max_length=2000)
    kind: str = "http"
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    binding: dict[str, Any] = Field(default_factory=dict)
    risk: str = "read"
    approval: dict[str, Any] = Field(default_factory=dict)
    roles: list[str] = Field(default_factory=lambda: ["customer", "agent", "admin"])
    flags: dict[str, Any] = Field(default_factory=dict)
    hints: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = False


class EnabledIn(BaseModel):
    enabled: bool


def tool_out(row: TenantTool) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "label": row.label, "description": row.description, "kind": row.kind,
            "parameters": row.parameters, "binding": row.binding, "mcp_server_id": row.mcp_server_id,
            "remote_name": row.remote_name, "risk": row.risk, "approval": row.approval, "roles": row.roles,
            "flags": row.flags, "hints": row.hints, "enabled": row.enabled, "status": row.status,
            "version": row.version, "updated_at": iso(row.updated_at)}


def _names(db: Session, tenant_id: int) -> set[str]:
    return set(db.execute(select(TenantTool.name).where(TenantTool.tenant_id == tenant_id)).scalars())


def _builtins() -> set[str]:
    return {t.name for t in get_services().registry.all()}


def _approvals(db: Session, tenant_id: int, name: str, pending_only: bool = False) -> list[int]:
    query = select(Approval.id).where(Approval.tenant_id == tenant_id, Approval.tool_name == name)
    if pending_only:
        query = query.where(Approval.status == "pending")
    return list(db.execute(query.order_by(Approval.id)).scalars())


STATS_DAYS = 30
_FAILED = ("rejected", "error")


def _stats(db: Session, tenant_id: int) -> dict[str, dict[str, Any]]:
    """Each tool's calls in the last 30 days, from the trace (customer and staff conversations, not evaluation
    runs), and its latest failure: what the console's tool list shows (N-231)."""
    since = datetime.now(timezone.utc) - timedelta(days=STATS_DAYS)
    live = (Span.tenant_id == tenant_id, Span.kind == "tool", Span.started_at >= since, Run.is_eval.is_(False))
    stats: dict[str, dict[str, Any]] = {}
    for name, calls, failed, last in db.execute(
            select(Span.name, func.count(Span.id), func.sum(case((Span.status.in_(_FAILED), 1), else_=0)),
                   func.max(Span.started_at))
            .join(Run, Run.id == Span.run_id).where(*live).group_by(Span.name)).all():
        stats[name] = {"calls": int(calls), "errors": int(failed or 0), "last_called_at": iso(last),
                       "last_error": None, "last_error_at": None}
    for name, output, at in db.execute(
            select(Span.name, Span.output, Span.started_at).join(Run, Run.id == Span.run_id)
            .where(*live, Span.status.in_(_FAILED)).order_by(Span.id.desc()).limit(500)).all():
        entry = stats.get(name)
        if entry is not None and entry["last_error"] is None:
            entry["last_error"] = str(((output or {}).get("output") or {}).get("error") or (output or {}).get("exception")
                                      or "调用失败")[:200]
            entry["last_error_at"] = iso(at)
    return stats


def _tiers(principal: Principal) -> dict[str, Any]:
    tenant = principal.tenant
    return {"currency": tenant.currency or "CNY", "auto_refund_limit": tenant.auto_refund_limit,
            "approval_admin_threshold": tenant.approval_admin_threshold}


@router.get("/tools")
def list_tools(principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(select(TenantTool).where(TenantTool.tenant_id == principal.tenant.id)
                      .order_by(TenantTool.id)).scalars().all()
    calls = dict(db.execute(select(ToolExecution.tool_name, func.count(ToolExecution.id))
                            .where(ToolExecution.tenant_id == principal.tenant.id)
                            .group_by(ToolExecution.tool_name)).all())
    waiting = dict(db.execute(select(Approval.tool_name, func.count(Approval.id))
                              .where(Approval.tenant_id == principal.tenant.id, Approval.status == "pending")
                              .group_by(Approval.tool_name)).all())
    stats = _stats(db, principal.tenant.id)
    empty = {"calls": 0, "errors": 0, "last_called_at": None, "last_error": None, "last_error_at": None}
    builtin = [{"name": t.name, "label": t.label or t.name, "description": t.description, "kind": "builtin",
                "risk": BUILTIN_RISK.get(t.risk, t.risk), "roles": list(t.roles), "stats": stats.get(t.name, empty),
                "pending_approvals": waiting.get(t.name, 0)} for t in get_services().registry.all()]
    return {"tools": [{**tool_out(r), "write_calls": calls.get(r.name, 0), "stats": stats.get(r.name, empty),
                       "pending_approvals": waiting.get(r.name, 0)} for r in rows], "builtin": builtin,
            "tool_hosts": list(principal.tenant.tool_hosts or []),
            "data_residency": principal.tenant.data_residency,
            "allowed_regions": list(principal.tenant.allowed_regions or []),
            "tiers": _tiers(principal), "stats_days": STATS_DAYS}


@router.post("/tools", status_code=status.HTTP_201_CREATED)
def create_tool(body: ToolIn, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    if body.kind != "http":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MCP 工具由同步 MCP Server 产生，不能手工新建")
    names = _names(db, principal.tenant.id)
    if body.name in names:
        raise HTTPException(status.HTTP_409_CONFLICT, f"工具 {body.name} 已存在")
    try:
        fields = check_definition(principal.tenant, body.model_dump(), _builtins(), _builtins() | names)
    except DefinitionError as exc:
        raise HTTPException(422, str(exc)) from None
    row = TenantTool(tenant_id=principal.tenant.id, enabled=body.enabled, updated_by=principal.user.id, **fields)
    db.add(row)
    db.flush()
    record(db, principal, "tool.create", f"tool:{row.id}", name=row.name, risk=row.risk, approval=row.approval,
           url=(row.binding or {}).get("url"), roles=row.roles, enabled=row.enabled)
    db.commit()
    return tool_out(row)


@router.put("/tools/{tool_id}")
def update_tool(tool_id: int, body: ToolIn, principal: Principal = Depends(require_admin),
                db: Session = Depends(get_db)) -> dict:
    row = get_owned(db, TenantTool, tool_id, principal.tenant.id)
    if body.name != row.name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "工具名不能修改（审批单、账本和评测用例按名字引用它）")
    data = body.model_dump()
    if row.kind == "mcp":  # what the server says the tool is stays the server's; the policy is ours
        data.update(kind="mcp", description=row.description, parameters=row.parameters)
    names = _names(db, principal.tenant.id)
    try:
        fields = check_definition(principal.tenant, data, _builtins(), _builtins() | names)
    except DefinitionError as exc:
        raise HTTPException(422, str(exc)) from None
    if row.kind == "mcp":
        fields.pop("binding", None)
        fields["kind"] = "mcp"
    if body.enabled and row.status == "held":
        raise HTTPException(status.HTTP_409_CONFLICT, f"这个 MCP 工具被拦下：{(row.hints or {}).get('held_reason', '')}")
    before = tool_out(row)
    for key, value in fields.items():
        setattr(row, key, value)
    row.enabled = body.enabled
    if row.kind == "mcp" and row.status == "needs_review":
        row.status = "active"  # saving the policy for the definition shown is the review
    row.version += 1
    row.updated_by = principal.user.id
    diff = changes(before, tool_out(row))
    diff.pop("updated_at", None)
    record(db, principal, "tool.update", f"tool:{row.id}", name=row.name, version=row.version,
           changes={k: v for k, v in diff.items() if k != "version"})
    db.commit()
    return tool_out(row)


@router.patch("/tools/{tool_id}")
def set_enabled(tool_id: int, body: EnabledIn, principal: Principal = Depends(require_admin),
                db: Session = Depends(get_db)) -> dict:
    """Switch one tool on or off: off takes effect on the next turn of every conversation."""
    row = get_owned(db, TenantTool, tool_id, principal.tenant.id)
    if body.enabled and row.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "这个 MCP 工具的定义还没有复核：请打开它，确认风险等级和审批策略后保存")
    if row.enabled != body.enabled:
        row.enabled = body.enabled
        row.version += 1
        record(db, principal, "tool.update", f"tool:{row.id}", name=row.name, version=row.version,
               changes={"enabled": [not body.enabled, body.enabled]})
        db.commit()
    # Switching off is never refused (it is how a misbehaving tool is stopped), but its waiting applications
    # are named: approving one now is refused, rejecting one tells the customer (N-230).
    waiting = [] if body.enabled else _approvals(db, principal.tenant.id, row.name, pending_only=True)
    return {**tool_out(row), "pending_approvals": waiting}


@router.delete("/tools/{tool_id}")
def delete_tool(tool_id: int, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    row = get_owned(db, TenantTool, tool_id, principal.tenant.id)
    used = db.execute(select(func.count(ToolExecution.id)).where(ToolExecution.tool_id == row.id)).scalar_one()
    if used:
        raise HTTPException(status.HTTP_409_CONFLICT, f"这个工具已有 {used} 条调用记录，只能停用，不能删除")
    # An approval names the tool it would run: its definition stays for the reviewer and for reconciliation (N-230).
    referenced = _approvals(db, principal.tenant.id, row.name)
    if referenced:
        raise HTTPException(status.HTTP_409_CONFLICT, f"有 {len(referenced)} 张审批单引用这个工具，只能停用，不能删除")
    record(db, principal, "tool.delete", f"tool:{row.id}", name=row.name)
    db.delete(row)
    db.commit()
    return {"deleted": True}



# ---------------------------------------------------------------- MCP servers
class McpServerIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,23}$")
    url: str = Field(max_length=512)
    auth: dict[str, Any] = Field(default_factory=dict)
    region: str = Field(default="", max_length=16)
    enabled: bool = True


def server_out(db: Session, server: McpServer) -> dict[str, Any]:
    tools = db.execute(select(TenantTool).where(TenantTool.mcp_server_id == server.id).order_by(TenantTool.id)).scalars()
    return {"id": server.id, "name": server.name, "url": server.url, "auth": server.auth, "region": server.region,
            "enabled": server.enabled, "last_synced_at": iso(server.last_synced_at), "last_error": server.last_error,
            "tools": [{"id": t.id, "name": t.name, "remote_name": t.remote_name, "enabled": t.enabled,
                       "status": t.status, "risk": t.risk} for t in tools]}


def _check_server(principal: Principal, body: McpServerIn) -> None:
    try:
        EgressPolicy(principal.tenant.tool_hosts or ()).check_template(body.url)
    except EgressError as exc:
        raise HTTPException(422, f"url 不被允许：{exc}") from None
    if body.auth:
        try:
            secrets.check_name(str(body.auth.get("secret") or ""))
        except secrets.SecretError as exc:
            raise HTTPException(422, str(exc)) from None
        scheme = body.auth.get("scheme", "bearer")
        if scheme not in ("bearer", "header") or (
                scheme == "header" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{1,40}", str(body.auth.get("header") or ""))):
            raise HTTPException(422, "auth.scheme 只能是 bearer，或 header 加合法的 auth.header 请求头名")
    # Arguments carry what the customer said: an MCP server receives personal data.
    problem = residency(principal.tenant, body.region, personal=True)
    if problem:
        raise HTTPException(422, problem)


@router.get("/mcp-servers")
def list_servers(principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> list[dict]:
    servers = db.execute(select(McpServer).where(McpServer.tenant_id == principal.tenant.id)
                         .order_by(McpServer.id)).scalars().all()
    return [server_out(db, s) for s in servers]


@router.post("/mcp-servers", status_code=status.HTTP_201_CREATED)
def create_server(body: McpServerIn, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    _check_server(principal, body)
    if db.execute(select(McpServer.id).where(McpServer.tenant_id == principal.tenant.id,
                                             McpServer.name == body.name)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, f"MCP Server {body.name} 已存在")
    server = McpServer(tenant_id=principal.tenant.id, **body.model_dump())
    db.add(server)
    db.flush()
    record(db, principal, "mcp.create", f"mcp:{server.id}", name=server.name, url=server.url, region=server.region)
    db.commit()
    return server_out(db, server)


@router.post("/mcp-servers/{server_id}/sync")
def sync(server_id: int, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """tools/list now: new tools arrive switched off; a changed definition switches its tool off."""
    server = get_owned(db, McpServer, server_id, principal.tenant.id)
    try:
        report = sync_server(db, principal.tenant, server, get_services().commerce)
    except McpError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"同步失败：{exc}") from None
    record(db, principal, "mcp.sync", f"mcp:{server.id}", **{k: v for k, v in report.items() if v})
    db.commit()
    return {"report": report, "server": server_out(db, server)}


@router.patch("/mcp-servers/{server_id}")
def set_server_enabled(server_id: int, body: EnabledIn, principal: Principal = Depends(require_admin),
                       db: Session = Depends(get_db)) -> dict:
    server = get_owned(db, McpServer, server_id, principal.tenant.id)
    if server.enabled != body.enabled:
        server.enabled = body.enabled
        record(db, principal, "mcp.update", f"mcp:{server.id}", changes={"enabled": [not body.enabled, body.enabled]})
        db.commit()
    return server_out(db, server)
