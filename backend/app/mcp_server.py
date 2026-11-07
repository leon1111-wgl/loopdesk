"""Expose LoopDesk's read-only tools over MCP (Model Context Protocol, stdio).

Any MCP client (Claude Desktop, Cursor, an internal agent) can then search the
knowledge base, look up orders and list a customer's orders with the same permission checks as
the web agent, from the same order system (app/integrations).
Write tools (refunds, tickets) are deliberately not exposed.

    LOOPDESK_MCP_USER=agent@leon.demo python -m app.mcp_server

Client config example (Claude Desktop ``mcpServers``):
    "loopdesk": {"command": "/path/to/backend/.venv/bin/python", "args": ["-m", "app.mcp_server"],
                 "cwd": "/path/to/backend", "env": {"LOOPDESK_MCP_USER": "agent@leon.demo"}}
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from sqlalchemy import select

from .agent.business_tools import get_order, list_orders, search_knowledge, track_shipment
from .agent.tools import ToolContext, ToolError
from .config import get_settings
from .db import init_engine, new_session, prepare_database
from .models import Tenant, User
from .services import Services


def _output(services: Services, handler, args: dict) -> dict:
    email = os.environ.get("LOOPDESK_MCP_USER", "agent@leon.demo")
    with new_session() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            return {"error": f"MCP 身份 {email} 不存在"}
        if user.disabled_at is not None:  # a member of staff who left keeps their laptop's MCP config
            return {"error": f"MCP 身份 {email} 已停用"}
        tenant = db.get(Tenant, user.tenant_id)
        ctx = ToolContext(db=db, settings=services.settings, tenant=tenant, user=user,
                          embedder=services.embedder, reranker=services.reranker, run_id=uuid.uuid4().hex,
                          conversation_id=None, dry_run=True,
                          orders=services.commerce.orders(tenant, db), refunds=services.commerce.refunds(tenant, db))
        try:
            output = handler(ctx, args)
        except ToolError as exc:
            output = {"error": str(exc)}
        output.pop("_debug", None)
        return output


def _call(services: Services, handler, args: dict) -> str:
    return json.dumps(_output(services, handler, args), ensure_ascii=False)


def build_server():
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    settings = get_settings()
    init_engine(settings.database_url)
    prepare_database(settings.database_url, settings.auto_migrate, settings.external_migrations)
    services = Services(settings)
    server = MCPServer("loopdesk")
    # What the tools say about themselves (MCP 2026-07-28: a hint a client may show, never what it should trust).
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    # Structured results: a client reads fields, and a text copy still goes along for those that do not.
    @server.tool(annotations=read_only)
    def loopdesk_search_knowledge(query: str, top_k: int = 4) -> dict[str, Any]:
        """检索 LoopDesk 企业知识库，返回带编号和相关度的片段（按 MCP 身份做可见性过滤）。"""
        return _output(services, search_knowledge, {"query": query, "top_k": top_k})

    @server.tool(annotations=read_only)
    def loopdesk_get_order(order_id: str) -> dict[str, Any]:
        """按订单号查询订单状态、金额和是否可退款。"""
        return _output(services, get_order, {"order_id": order_id})

    @server.tool(annotations=read_only)
    def loopdesk_list_orders(customer_email: str = "") -> dict[str, Any]:
        """列出一位顾客最近的订单（员工按邮箱查；顾客身份只能看自己的）。"""
        return _output(services, list_orders, {"customer_email": customer_email})

    @server.tool(annotations=read_only)
    def loopdesk_track_shipment(order_id: str) -> dict[str, Any]:
        """查询订单物流进度。"""
        return _output(services, track_shipment, {"order_id": order_id})

    return server


if __name__ == "__main__":  # pragma: no cover
    build_server().run("stdio")
