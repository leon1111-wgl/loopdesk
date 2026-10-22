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

from sqlalchemy import select

from .agent.business_tools import get_order, list_orders, search_knowledge, track_shipment
from .agent.tools import ToolContext, ToolError
from .config import get_settings
from .db import init_engine, new_session, prepare_database
from .models import Tenant, User
from .services import Services


def _call(services: Services, handler, args: dict) -> str:
    email = os.environ.get("LOOPDESK_MCP_USER", "agent@leon.demo")
    with new_session() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            return json.dumps({"error": f"MCP 身份 {email} 不存在"}, ensure_ascii=False)
        if user.disabled_at is not None:  # a member of staff who left keeps their laptop's MCP config
            return json.dumps({"error": f"MCP 身份 {email} 已停用"}, ensure_ascii=False)
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
        return json.dumps(output, ensure_ascii=False)


def build_server():
    from mcp.server.mcpserver import MCPServer

    settings = get_settings()
    init_engine(settings.database_url)
    prepare_database(settings.database_url, settings.auto_migrate)
    services = Services(settings)
    server = MCPServer("loopdesk")

    @server.tool()
    def loopdesk_search_knowledge(query: str, top_k: int = 4) -> str:
        """检索 LoopDesk 企业知识库，返回带编号和相关度的片段（按 MCP 身份做可见性过滤）。"""
        return _call(services, search_knowledge, {"query": query, "top_k": top_k})

    @server.tool()
    def loopdesk_get_order(order_id: str) -> str:
        """按订单号查询订单状态、金额和是否可退款。"""
        return _call(services, get_order, {"order_id": order_id})

    @server.tool()
    def loopdesk_list_orders(customer_email: str = "") -> str:
        """列出一位顾客最近的订单（员工按邮箱查；顾客身份只能看自己的）。"""
        return _call(services, list_orders, {"customer_email": customer_email})

    @server.tool()
    def loopdesk_track_shipment(order_id: str) -> str:
        """查询订单物流进度。"""
        return _call(services, track_shipment, {"order_id": order_id})

    return server


if __name__ == "__main__":  # pragma: no cover
    build_server().run("stdio")
