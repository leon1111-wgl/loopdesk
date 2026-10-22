"""A tenant attaches an external MCP server; its tools come in under the same risk policy.

Transport: Streamable HTTP only (JSON-RPC 2.0 over POST; the reply is JSON or an SSE stream). A
stdio server would mean a tenant administrator choosing a program for the platform to run: no.
The server's URL goes through the same egress policy as any tenant tool (https, the tenant's
allowlist, public addresses only, the checked address pinned).

What an MCP server says about its tools is not trusted either:
- a synced tool arrives disabled, as a write tool needing approval, until an administrator has read
  it and set its risk tier and approval policy (the tool's own annotations are hints, not policy);
- its description and parameter descriptions are prompt text the model will read: a description that
  instructs the model (tool poisoning) is held back, as a poisoned knowledge chunk is;
- its input schema must fit the subset the runtime enforces (tooling/schema.py), or it stays off;
- the reviewed definition is hashed; when a later sync finds it changed (a "rug pull": approved as
  "get the weather", now "and send the conversation to x"), the tool is switched off until someone
  reviews it again;
- results are scanned like every tool's output before the model sees them (runtime).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.tools import ToolContext, ToolError, ToolUnavailable
from ..guardrails.injection import Channels, scan_knowledge
from ..integrations.base import record
from ..models import McpServer, TenantTool
from . import secrets
from .egress import EgressError, EgressPolicy
from .http_tool import HttpToolCaller, NotSent, _Reply
from .schema import NAME_RE, SchemaError, validate_schema

PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = 8.0
MAX_TOOLS = 50
MAX_PAGES = 5
MAX_REPLY_BYTES = 256 * 1024


class McpError(Exception):
    pass


def definition_hash(tool: dict[str, Any]) -> str:
    content = json.dumps({"name": tool.get("name"), "description": tool.get("description", ""),
                          "inputSchema": tool.get("inputSchema") or {}}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode()).hexdigest()


def local_name(server: str, remote: str) -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", f"{server}__{remote}".lower()).strip("_")
    return name[:48] if NAME_RE.match(name[:48]) else f"mcp_{name}"[:48]


class McpSession:
    """One JSON-RPC conversation with a server: initialize, then requests carrying its session id."""

    def __init__(self, client: httpx.Client, pin: EgressPolicy | None, url: str, headers: dict[str, str]):
        self.client, self.pin, self.url, self.headers = client, pin, url, headers
        self.session_id: str | None = None
        self._next = 0

    def initialize(self) -> dict[str, Any]:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "LoopDesk", "version": "1.0"}})
        self.notify("notifications/initialized")
        return result

    def notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next += 1
        ident = self._next
        response = self._post({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
        if response.status_code >= 400:
            raise McpError(f"{method}: HTTP {response.status_code}")
        if response.headers.get("mcp-session-id"):
            self.session_id = response.headers["mcp-session-id"]
        message = _message(response, ident)
        if "error" in message:
            raise McpError(f"{method}: {str((message['error'] or {}).get('message', 'error'))[:200]}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpError(f"{method}: the reply has no result")
        return result

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {**self.headers, "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json", "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = self.client.build_request("POST", self.url, content=json.dumps(payload, ensure_ascii=False).encode(),
                                            headers=headers, timeout=httpx.Timeout(TIMEOUT, connect=3.0))
        if self.pin is not None:
            request = self.pin.pin(request)
        response = self.client.send(request, stream=True)
        try:
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_REPLY_BYTES:
                    raise McpError("the reply is larger than 256 KB")
        finally:
            response.close()
        return httpx.Response(response.status_code, headers=response.headers, content=body)


def _message(response: httpx.Response, ident: int) -> dict[str, Any]:
    """The JSON-RPC reply with our id, from a JSON body or an SSE stream."""
    kind = response.headers.get("content-type", "")
    candidates: list[Any] = []
    if "text/event-stream" in kind:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                try:
                    candidates.append(json.loads(line[5:].strip()))
                except ValueError:
                    continue
    else:
        try:
            candidates.append(response.json())
        except ValueError:
            raise McpError("the reply is not JSON") from None
    for item in candidates:
        for message in item if isinstance(item, list) else [item]:
            if isinstance(message, dict) and message.get("id") == ident:
                return message
    raise McpError("no reply to the request")


def _session(commerce: Any, tenant: Any, server: McpServer) -> McpSession:
    try:
        host = EgressPolicy(tenant.tool_hosts or ()).check_template(server.url)
    except EgressError as exc:
        raise NotSent(f"MCP Server {server.name} 不被出站策略允许：{exc}") from None
    headers = {"User-Agent": "LoopDesk/1.0", "X-LoopDesk-Tenant": tenant.slug}
    auth = server.auth or {}
    if auth.get("secret"):
        try:
            value = secrets.resolve(auth["secret"], tenant.slug)
        except secrets.SecretError as exc:
            raise NotSent(f"MCP Server {server.name} 的{exc}") from None
        if auth.get("scheme", "bearer") == "bearer":
            headers["Authorization"] = f"Bearer {value}"
        else:
            headers[auth["header"]] = value
    client, pin = commerce.tool_client(tenant, host)
    return McpSession(client, pin, server.url, headers)


# ---------------------------------------------------------------- sync
def sync_server(db: Session, tenant: Any, server: McpServer, commerce: Any) -> dict[str, list[str]]:
    """tools/list -> TenantTool rows. New tools arrive off; changed ones are switched off."""
    report: dict[str, list[str]] = {"added": [], "changed": [], "unchanged": [], "held": [], "removed": []}
    try:
        session = _session(commerce, tenant, server)
        session.initialize()
        listed: list[dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_PAGES):  # tools/list pages with nextCursor
            page = session.request("tools/list", {"cursor": cursor} if cursor else {})
            listed += [t for t in page.get("tools") or [] if isinstance(t, dict)]
            cursor = page.get("nextCursor")
            if not cursor or len(listed) >= MAX_TOOLS:
                break
    except (McpError, httpx.HTTPError, EgressError, NotSent) as exc:
        server.last_error = str(exc)[:500]
        db.commit()
        raise McpError(str(exc)) from None
    channels = Channels.of(tenant.official_domains, tenant.official_contacts)
    existing = {t.remote_name: t for t in db.execute(select(TenantTool).where(
        TenantTool.tenant_id == tenant.id, TenantTool.mcp_server_id == server.id)).scalars()}
    taken = set(db.execute(select(TenantTool.name).where(TenantTool.tenant_id == tenant.id)).scalars())
    seen: set[str] = set()
    for tool in listed[:MAX_TOOLS]:
        remote = str(tool.get("name") or "")[:128]
        if not remote:
            continue
        seen.add(remote)
        problem = _problem(tool, channels)
        digest = definition_hash(tool)
        row = existing.get(remote)
        if row is None:
            name = local_name(server.name, remote)
            suffix = 2
            while name in taken:  # "fx-rate" and "fx_rate", or an HTTP tool of the same name
                name = f"{local_name(server.name, remote)[:44]}_{suffix}"
                suffix += 1
            taken.add(name)
            row = TenantTool(tenant_id=tenant.id, name=name, kind="mcp",
                             mcp_server_id=server.id, remote_name=remote, label=remote[:32],
                             risk="write", approval={"mode": "always", "approver": "agent"},
                             roles=["customer", "agent", "admin"], flags={"region": server.region},
                             hints={}, enabled=False, binding={})
            db.add(row)
            report["added"].append(row.name)
        elif row.definition_hash == digest:  # as reviewed (or as still waiting for review, or held)
            report["unchanged"].append(row.name)
            continue
        else:
            row.enabled = False  # what an administrator approved is not what the server now says
            report["changed"].append(row.name)
        row.description = str(tool.get("description") or "")[:1000]
        row.parameters = _schema(tool)
        row.definition_hash = digest
        row.status = "needs_review"
        row.version = (row.version or 0) + 1
        if problem:
            row.status = "held"
            row.hints = {**(row.hints or {}), "held_reason": problem}
            report["held"].append(row.name)
    for remote, row in existing.items():
        if remote not in seen and row.enabled:
            row.enabled = False
            row.status = "needs_review"
            report["removed"].append(row.name)
    server.last_synced_at = datetime.now(timezone.utc)
    server.last_error = ""
    return report


def _schema(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
    return schema if isinstance(schema, dict) else {"type": "object", "properties": {}}


def _problem(tool: dict[str, Any], channels: Channels) -> str | None:
    texts = [str(tool.get("description") or "")]
    schema = _schema(tool)
    texts += [str(p.get("description") or "") for p in (schema.get("properties") or {}).values() if isinstance(p, dict)]
    for text in texts:
        reasons = scan_knowledge(text, public=False, channels=channels)
        if reasons:
            return f"工具说明里有像是写给模型的指令或诈骗话术（{', '.join(reasons)}）"
    try:
        validate_schema(schema)
    except SchemaError as exc:
        return f"参数定义超出平台能校验的范围：{exc}"
    return None


# ---------------------------------------------------------------- calling
class McpToolCaller(HttpToolCaller):
    """The HTTP caller's rules (sandbox, dry run, ledger, unknown outcomes), over tools/call. MCP has
    no idempotency key: a write is sent once and never retried."""

    def __init__(self, ctx: ToolContext, row: TenantTool, label: str, server: McpServer):
        super().__init__(ctx, row, label)
        self.server = server
        self.binding = {"url": server.url, "idempotency": "none", "errors": (row.binding or {}).get("errors") or {}}

    def _secrets(self) -> None:
        self._auth, self._signing = {}, None  # the session carries the server's credentials

    def _send(self, args: dict[str, Any], host: str, key: str | None) -> _Reply:
        entry = record(system=f"mcp:{self.server.name}", call=self.row.remote_name, via="mcp", method="POST", path=host)
        started = time.perf_counter()
        try:
            session = _session(self.commerce, self.ctx.tenant, self.server)
            session.headers.update(self._identity())
            session.initialize()
            result = session.request("tools/call", {"name": self.row.remote_name, "arguments": args})
        except NotSent:
            raise
        except EgressError as exc:
            raise NotSent(f"{self.label}暂时不可用（出站策略：{exc}）") from None
        except (McpError, httpx.HTTPError) as exc:
            entry.update(attempts=1, error=str(exc)[:200], latency_ms=int((time.perf_counter() - started) * 1000))
            raise ToolUnavailable(f"{self.label}所需的外部服务暂时无法访问，请稍后再试或转人工客服") from None
        entry.update(status=200, attempts=1, latency_ms=int((time.perf_counter() - started) * 1000))
        data = result.get("structuredContent")
        if not isinstance(data, dict):
            text = "".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
            try:
                data = json.loads(text)
            except ValueError:
                data = {"text": text[:2000]}
            if not isinstance(data, dict):
                data = {"value": data}
        if result.get("isError"):  # the tool ran and failed: a refusal, not an unknown outcome
            entry.update(error="isError")
            return _Reply(400, json.dumps({"code": str(data.get("code") or "tool_error")}).encode(), entry)
        return _Reply(200, json.dumps(data, ensure_ascii=False).encode(), entry)

    def _identity(self) -> dict[str, str]:
        headers = self._headers(None)
        return {k: v for k, v in headers.items() if k.startswith("X-LoopDesk") or k == "X-Request-Id"}

    def _host(self) -> str:
        host = urlsplit(self.server.url).hostname or ""
        try:
            EgressPolicy(self.ctx.tenant.tool_hosts or ()).check_template(self.server.url)
        except EgressError as exc:
            raise NotSent(f"{self.label}暂时不可用（出站策略：{exc}）") from None
        return host


def call_mcp_tool(ctx: ToolContext, row: TenantTool, label: str, args: dict[str, Any]) -> dict[str, Any]:
    server = ctx.db.get(McpServer, row.mcp_server_id) if row.mcp_server_id else None
    if server is None or not server.enabled or server.tenant_id != ctx.tenant.id:
        raise ToolError(f"{label}所在的 MCP Server 已停用")
    return McpToolCaller(ctx, row, label, server)(args)
