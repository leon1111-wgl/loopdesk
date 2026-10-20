"""The six business tools the support agent can call.

Keys starting with "_" in a tool result are trace-only: the runtime records them in the
span and strips them before the result reaches the model.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from ..models import Approval, Conversation, Order, Refund, Tenant, Ticket
from ..rag.conflicts import disputed
from ..rag.retrieval import hybrid_search
from ..textutil import normalize_width
from . import handoff
from .tools import ApprovalNeed, ToolContext, ToolDef, ToolError, ToolRegistry

STATUS_TEXT = {
    "paid": "已付款，待发货",
    "shipped": "已发货",
    "delivered": "已签收",
    "refunding": "退款中",
    "refunded": "已退款",
}
REFUNDABLE = {"paid", "shipped", "delivered"}
PRIORITY_TEXT = {"low": "低", "normal": "普通", "high": "高", "urgent": "紧急"}


def _order_id(args: dict[str, Any]) -> str:
    value = normalize_width(str(args.get("order_id") or "")).strip().upper()
    if not value:
        raise ToolError("缺少订单号")
    return value


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _days_since_delivery(order: Order, at: datetime) -> int | None:
    """Whole days since delivery, the unit the policy is written in ("自签收之日起7天内");
    None while the order has not been delivered (refunding before delivery is not time-limited)."""
    if order.delivered_at is None:
        return None
    return max(0, (at - _as_utc(order.delivered_at)).days)


def _last_refund_day(tenant: Tenant) -> int:
    """After this many days since delivery no refund can be filed online at all."""
    return max(tenant.return_window_days, tenant.quality_window_days)


def _load_order(ctx: ToolContext, order_id: str) -> Order:
    order = ctx.db.get(Order, order_id)
    # Same message for "missing" and "not yours": do not reveal that an order exists.
    if order is None or order.tenant_id != ctx.tenant.id or (not ctx.is_staff and order.customer_id != ctx.user.id):
        raise ToolError("订单不存在或无权查看")
    return order


# ---------------------------------------------------------------- search_knowledge
def search_knowledge(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query 不能为空")
    top_k = max(1, min(int(args.get("top_k") or ctx.settings.retrieval_top_k), 8))
    scope = {"public", "internal"} if ctx.is_staff else {"public"}
    hits, debug = hybrid_search(
        ctx.db, ctx.tenant.id, query, ctx.embedder, scope,
        top_k=top_k, min_relevance=ctx.settings.min_relevance,
    )
    # Relative cut-off: when one chunk clearly answers the question, weaker neighbours
    # only add noise (and tokens) to the prompt and to the reviewer's evidence package.
    if hits:
        floor = 0.5 * max(h.relevance for h in hits)
        hits = [h for h in hits if h.relevance >= floor]
    results = []
    for hit in hits:
        evidence = ctx.add_evidence(
            "kb", hit.doc_title, hit.text,
            heading=hit.heading, chunk_id=hit.chunk_id, document_id=hit.document_id,
            page=hit.page, relevance=round(hit.relevance, 3),
        )
        results.append({
            "ref": evidence.ref, "doc": hit.doc_title, "heading": hit.heading,
            "text": hit.text, "relevance": round(hit.relevance, 3), "page": hit.page,
        })
    output: dict[str, Any] = {"query": query, "results": results}
    if not results:
        output["note"] = "知识库中没有足够相关的内容"
    # Open conflicts with both sides in this evidence: the runtime hands over instead of
    # letting the model state both versions.
    conflicts = disputed(ctx.db, ctx.tenant.id, [hit.text for hit in hits])
    if conflicts:
        output["disputed"] = [{"conflict_id": c.id, "sentence": c.sentence, "other_sentence": c.other_sentence}
                              for c in conflicts]
    output["_debug"] = {
        "scope": sorted(scope),
        "query_tokens": debug.query_tokens,
        "dropped_injection_chunks": debug.dropped_flagged,
        "filtered_low_relevance": debug.filtered_low_relevance,
        "hits": [h.as_dict() for h in hits],
    }
    return output


# ---------------------------------------------------------------- orders
def get_order(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    order = _load_order(ctx, _order_id(args))
    status_text = STATUS_TEXT.get(order.status, order.status)
    refundable = order.status in REFUNDABLE
    refund_note = "" if refundable else f"该订单当前状态为“{status_text}”，不能重复申请退款"
    days = _days_since_delivery(order, datetime.now(timezone.utc))
    if refundable and days is not None:
        tenant = ctx.tenant
        if days > _last_refund_day(tenant):
            refundable = False
            refund_note = f"订单已签收 {days} 天，超过 {_last_refund_day(tenant)} 天的售后期限"
        elif days > tenant.return_window_days:
            refund_note = (f"订单已签收 {days} 天，已超过 {tenant.return_window_days} 天无理由退货期限，"
                           f"{tenant.quality_window_days} 天内仅质量问题可以退换，提交后需要人工核实")
    return {
        "order_id": order.id,
        "status": order.status,
        "status_text": status_text,
        "amount": round(order.amount, 2),
        "items": order.items,
        "paid_at": order.paid_at.strftime("%Y-%m-%d"),
        "delivered_at": order.delivered_at.strftime("%Y-%m-%d") if order.delivered_at else None,
        "days_since_delivery": days,
        "region": order.region,
        "refundable": refundable,
        "refund_note": refund_note,
        "summary": f"订单 {order.id} 当前状态为“{status_text}”，订单金额 ¥{order.amount:.2f}",
    }


def track_shipment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    order = _load_order(ctx, _order_id(args))
    shipment = order.shipment or {}
    events = shipment.get("events") or []
    if order.status == "paid" or not events:
        return {"order_id": order.id, "shipped": False, "message": f"订单 {order.id} 目前还未发货。"}
    latest = events[-1]
    return {
        "order_id": order.id,
        "shipped": True,
        "carrier": shipment.get("carrier"),
        "tracking_no": shipment.get("tracking_no"),
        "events": events,
        "message": (
            f"订单 {order.id} 由{shipment.get('carrier')}承运，运单号 {shipment.get('tracking_no')}。"
            f"最新进度（{latest['time']}）：{latest['desc']}。"
        ),
    }


# ---------------------------------------------------------------- refunds (high risk)
def _refund_for_approval(ctx: ToolContext) -> Refund | None:
    """The approval id is the idempotency key: retrying an approved run must not move money twice."""
    if ctx.approval_id is None or ctx.dry_run:
        return None
    return ctx.db.execute(select(Refund).where(Refund.approval_id == ctx.approval_id)).scalar_one_or_none()


def _application_time(ctx: ToolContext) -> datetime:
    """When the customer asked. After a human approval the tool runs again, possibly days
    later; the after-sales window is judged at the request, so a slow review queue cannot
    push a valid request out of its window."""
    approval = ctx.db.get(Approval, ctx.approval_id) if ctx.approval_id is not None else None
    return _as_utc(approval.created_at) if approval is not None else datetime.now(timezone.utc)


def _refund_precheck(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    done = _refund_for_approval(ctx)
    if done is not None:  # already executed; the order is "refunding" now, so skip re-validation
        return {"order_id": done.order_id, "amount": done.amount}
    order = _load_order(ctx, _order_id(args))
    raw = args.get("amount")
    try:  # an omitted amount means the full paid amount (see the tool schema); 0 is not "omitted"
        amount = round(float(order.amount if raw is None or raw == "" else raw), 2)
    except (TypeError, ValueError) as exc:
        raise ToolError("退款金额格式不正确") from exc
    if not math.isfinite(amount):
        raise ToolError("退款金额格式不正确")
    if amount <= 0:
        raise ToolError("退款金额必须大于 0")
    if amount > order.amount + 1e-6:
        raise ToolError(f"退款金额 ¥{amount:.2f} 超过订单实付金额 ¥{order.amount:.2f}")
    if order.status not in REFUNDABLE:
        raise ToolError(f"该订单当前状态为“{STATUS_TEXT.get(order.status, order.status)}”，不能发起退款")
    days = _days_since_delivery(order, _application_time(ctx))
    if days is not None and days > _last_refund_day(ctx.tenant):
        # A hard rule, not an approval: the policy leaves nothing to decide. Exceptions
        # (goodwill, warranty) belong to a human in the back office, not to the agent.
        raise ToolError(f"订单已签收 {days} 天，超过 {_last_refund_day(ctx.tenant)} 天的售后期限，不能在线申请退款；"
                        "如有特殊情况，可以转人工客服处理")
    if ctx.approval_id is None:
        pending = ctx.db.execute(select(Approval).where(
            Approval.tenant_id == ctx.tenant.id, Approval.tool_name == "request_refund", Approval.status == "pending",
        )).scalars()
        if any(a.arguments.get("order_id") == order.id for a in pending):
            raise ToolError("该订单已有一笔退款申请在等待人工审核，请勿重复提交")
    return {"order_id": order.id, "amount": amount}


def _refund_history(ctx: ToolContext, customer_id: int) -> tuple[int, float]:
    """Refunds already executed for this customer inside the velocity window."""
    since = datetime.now(timezone.utc) - timedelta(days=ctx.settings.refund_window_days)
    count, total = ctx.db.execute(
        select(func.count(Refund.id), func.coalesce(func.sum(Refund.amount), 0.0))
        .join(Order, Order.id == Refund.order_id)
        .where(Refund.tenant_id == ctx.tenant.id, Order.customer_id == customer_id, Refund.created_at >= since)
    ).one()
    return int(count), float(total)


def _refund_policy(ctx: ToolContext, args: dict[str, Any]) -> ApprovalNeed | None:
    # args went through _refund_precheck, so "amount" is the amount that would be paid.
    amount = round(float(args["amount"]), 2)
    order = ctx.db.get(Order, _order_id(args))
    if order is None:
        return None
    tenant, settings = ctx.tenant, ctx.settings
    reasons: list[str] = []
    customer_note = ""

    # Between the no-reason and the quality window only a quality problem qualifies, and
    # the tool cannot look at the customer's photos: a person decides, whatever the amount.
    days = _days_since_delivery(order, datetime.now(timezone.utc))
    if days is not None and days > tenant.return_window_days:
        customer_note = (f"订单已签收 {days} 天，超过 {tenant.return_window_days} 天无理由退货期限，"
                         f"{tenant.quality_window_days} 天内仅质量问题可以退换")
        reasons.append(customer_note)  # published policy, so the customer may hear it too
        ctx.add_evidence("tool", "after_sales_window", json.dumps({
            "order_id": order.id, "delivered_at": _as_utc(order.delivered_at).strftime("%Y-%m-%d"),
            "days_since_delivery": days, "return_window_days": tenant.return_window_days,
            "quality_window_days": tenant.quality_window_days,
        }, ensure_ascii=False))

    limit = tenant.auto_refund_limit
    if amount > limit:
        reasons.append(f"超过自动审批额度 ¥{limit:.2f}")
    else:
        # Velocity check, counted per customer (not per operator: a support agent filing on
        # the customer's behalf must not reset the counter). It stops "many small refunds".
        window = settings.refund_window_days
        count, total = _refund_history(ctx, order.customer_id)
        velocity = None
        if count + 1 > settings.refund_window_max_count:
            velocity = (f"近 {window} 天已有 {count} 笔退款，超过自动审批的次数上限"
                        f"（{settings.refund_window_max_count} 笔）")
        elif total + amount > settings.refund_window_max_amount + 1e-6:
            velocity = (f"近 {window} 天累计退款将达到 ¥{total + amount:.2f}，"
                        f"超过 ¥{settings.refund_window_max_amount:.2f} 的累计自动审批上限")
        if velocity:
            reasons.append(velocity)
            # The reviewer sees the history that triggered the escalation in the evidence package.
            ctx.add_evidence("tool", "refund_history", json.dumps({
                "customer_id": order.customer_id, "window_days": window, "refund_count": count,
                "refund_total": round(total, 2), "this_request": amount,
            }, ensure_ascii=False))
    # Amount and velocity limits are risk controls: the reviewer sees them, the customer does not.
    return ApprovalNeed("；".join(reasons), customer_note) if reasons else None


def request_refund(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    done = _refund_for_approval(ctx)
    if done is not None:
        return {
            "status": "approved", "refund_id": done.id, "order_id": done.order_id, "amount": done.amount,
            "message": f"退款单 #{done.id} 已受理，退款金额 ¥{done.amount:.2f} 将按原支付方式退回。",
        }
    # Re-validate at execution time: the order may have changed while waiting for approval.
    checked = _refund_precheck(ctx, args)
    amount = checked["amount"]
    status = "approved" if ctx.approval_id else "auto_approved"
    if ctx.dry_run:
        return {
            "status": status, "refund_id": None, "order_id": checked["order_id"], "amount": amount,
            "message": f"（演练）退款申请已受理，退款金额 ¥{amount:.2f} 将按原支付方式退回。",
        }
    # Claim the order in one conditional UPDATE. The precheck above is a read: two requests
    # for the same order (a double click, a retry) can both pass it, and both used to pay
    # out. Only one UPDATE can move the order out of a refundable state; SQLite serialises
    # writers and PostgreSQL re-checks the condition after the row lock.
    claimed = ctx.db.execute(
        update(Order).where(Order.id == checked["order_id"], Order.status.in_(REFUNDABLE))
        .values(status="refunding").execution_options(synchronize_session="fetch")
    ).rowcount
    if claimed != 1:
        ctx.db.rollback()
        raise ToolError("该订单已有一笔退款正在处理，请勿重复提交")
    refund = Refund(
        tenant_id=ctx.tenant.id, order_id=checked["order_id"], amount=amount,
        reason=str(args.get("reason") or "")[:500], approval_id=ctx.approval_id,
    )
    ctx.db.add(refund)
    ctx.db.commit()
    return {
        "status": status, "refund_id": refund.id, "order_id": checked["order_id"], "amount": amount,
        "message": f"退款单 #{refund.id} 已受理，退款金额 ¥{amount:.2f} 将按原支付方式退回。",
    }


# ---------------------------------------------------------------- tickets & handoff
def create_ticket(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()[:120] or "客户问题"
    description = str(args.get("description") or "").strip()[:2000]
    priority = str(args.get("priority") or "normal")
    if priority not in PRIORITY_TEXT:
        priority = "normal"
    sla = 4 if priority in ("high", "urgent") else ctx.tenant.ticket_sla_hours
    if ctx.dry_run:
        ticket_ref, prefix = "", "（演练）"
    else:
        ticket = Ticket(
            tenant_id=ctx.tenant.id, conversation_id=ctx.conversation_id, customer_id=ctx.user.id,
            title=title, description=description, priority=priority,
            due_at=datetime.now(timezone.utc) + timedelta(hours=sla),  # what the customer is told below
        )
        ctx.db.add(ticket)
        ctx.db.commit()
        ticket_ref, prefix = f" #{ticket.id}", ""
    return {
        "ticket_id": None if ctx.dry_run else ticket.id,
        "priority": priority,
        "sla_hours": sla,
        "message": f"{prefix}已为您创建工单{ticket_ref}（优先级：{PRIORITY_TEXT[priority]}），技术支持将在 {sla} 小时内联系您。",
    }


def _already_queued(ctx: ToolContext, ticket: Ticket) -> dict[str, Any]:
    current = handoff.ticket_state(ctx.db, ticket)
    message = (handoff.notice(current) if current["assignee"] else
               f"您已在人工客服队列中，当前排第 {current['position']} 位，无需重复申请，请稍候。")
    return {"queued": True, "already_queued": True, "queue_position": current["position"], "message": message}


def handoff_to_human(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    reason = str(args.get("reason") or "用户要求转人工")[:200]
    if ctx.conversation_id:
        existing = handoff.active_ticket(ctx.db, ctx.conversation_id)
        if existing is not None:
            # Asking again must not open a second ticket or move the customer behind people who
            # queued later. (The runtime normally keeps the agent out of a conversation that is
            # with a person; this covers the agent being called anyway.)
            return _already_queued(ctx, existing)
    waiting = handoff.waiting_ahead(ctx.db, ctx.tenant.id)  # the new ticket is not in it yet
    if not ctx.dry_run:
        if ctx.conversation_id:
            conversation = ctx.db.get(Conversation, ctx.conversation_id)
            if conversation:
                conversation.status = "handoff"
        ctx.db.add(Ticket(
            tenant_id=ctx.tenant.id, conversation_id=ctx.conversation_id, customer_id=ctx.user.id,
            kind="handoff", title=f"转人工：{reason[:40]}", description=reason, priority="high",
        ))
        try:
            ctx.db.commit()
        except IntegrityError:  # a concurrent request opened this conversation's ticket first
            ctx.db.rollback()
            return _already_queued(ctx, handoff.active_ticket(ctx.db, ctx.conversation_id))
    position = waiting + 1
    return {"queued": True, "already_queued": False, "queue_position": position,
            "message": handoff.notice({"position": position, "assignee": None})}


# ---------------------------------------------------------------- registry
def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolDef(
        name="search_knowledge",
        description="检索企业知识库（售后政策、物流、发票、积分等）。回答任何政策、规则、时效类问题前必须先调用。返回带编号 ref 的片段，回答时用 [ref] 标注引用。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索语句，使用用户问题中的关键词"},
                "top_k": {"type": "integer", "description": "返回片段数，默认 4，最大 8"},
            },
            "required": ["query"],
        },
        handler=search_knowledge,
    ))
    registry.register(ToolDef(
        name="get_order",
        description="按订单号查询订单状态、金额、商品和是否可退款。顾客只能查询自己的订单。",
        parameters={
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "订单号，如 SO20261008002"}},
            "required": ["order_id"],
        },
        handler=get_order,
    ))
    registry.register(ToolDef(
        name="track_shipment",
        description="查询订单的物流承运商、运单号和最新物流进度。",
        parameters={
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "订单号"}},
            "required": ["order_id"],
        },
        handler=track_shipment,
    ))
    registry.register(ToolDef(
        name="request_refund",
        description="为订单发起退款。金额超过租户自动审批额度时会进入人工审批，审批结果出来前不要向用户承诺退款成功。",
        parameters={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "amount": {"type": "number", "description": "退款金额（元），默认订单实付金额"},
                "reason": {"type": "string", "description": "退款原因"},
            },
            "required": ["order_id", "reason"],
        },
        handler=request_refund,
        risk="high",
        precheck=_refund_precheck,
        approval=_refund_policy,
    ))
    registry.register(ToolDef(
        name="create_ticket",
        description="为无法在线解决的问题（故障、报修、复杂售后）创建工单，由技术支持跟进。",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "工单标题"},
                "description": {"type": "string", "description": "问题描述"},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
            },
            "required": ["title", "description"],
        },
        handler=create_ticket,
        risk="write",
    ))
    registry.register(ToolDef(
        name="handoff_to_human",
        description="把对话转接给人工客服。用户明确要求人工、投诉，或你无法依据证据回答时使用。",
        parameters={
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "转人工原因"}},
            "required": ["reason"],
        },
        handler=handoff_to_human,
        risk="write",
    ))
    return registry
