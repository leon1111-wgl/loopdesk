"""The seven business tools the support agent can call.

Keys starting with "_" in a tool result are trace-only: the runtime records them in the
span and strips them before the result reaches the model.

Orders, shipments and refunds come from the order and payment systems through ctx.orders and
ctx.refunds (app/integrations): the demo tables, or a merchant's own systems over HTTP. What the
tools decide (who may see an order, which refund needs a person, the velocity limit) and what they
record (the refund ledger) stay here, the same whichever system answers.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import audit
from ..integrations import (REFUNDABLE, GatewayError, OrderInfo, RefundGateway, RefundRejected, RefundRequest,
                            customer_ref, reads)
from ..localtime import local_date, zone_of
from ..models import Approval, Conversation, Message, Refund, Run, Tenant, Ticket, User
from ..rag.conflicts import disputed
from ..rag.retrieval import gate_options, hybrid_search
from ..security import Principal
from ..textutil import (REFUND_RE, normalize_width, refund_bare_decline, refund_declined, refund_requested,
                        refund_withdraw_requested)
from . import handoff
from .tools import ApprovalNeed, ApprovalRequired, ToolContext, ToolDef, ToolError, ToolRegistry, ToolUnavailable

STATUS_TEXT = {
    "paid": "已付款，待发货",
    "shipped": "已发货",
    "delivered": "已签收",
    "refunding": "退款中",
    "refunded": "已退款",
}
PRIORITY_TEXT = {"low": "低", "normal": "普通", "high": "高", "urgent": "紧急"}
NOT_FOUND_TEXT = "订单不存在或无权查看"
ORDER_SYSTEM_DOWN_TEXT = "订单系统暂时无法访问，请稍后再试或转人工客服"
# A refusal from the payment system, by its code. Its own message can name internal rules or
# accounts: that goes to the trace, the customer gets one of these.
REFUSED_TEXT = "支付系统没有受理这笔退款，请转人工客服处理"
REFUSAL_TEXT = {
    "order_not_refundable": "该订单在订单系统中当前不能退款",
    "amount_exceeds_refundable": "退款金额超过了该订单还可以退的金额",
    "order_not_found": "支付系统查不到这个订单，请转人工客服处理",
    "refund_declined": REFUSED_TEXT,
}
ONE_REFUND_TEXT = "每个订单只能在线申请一次退款，如需再退请转人工客服"
PENDING_TEXT = ("退款申请已提交（退款单 #{id}），支付系统暂未返回结果，我们会继续核实，请勿重复申请；"
                "处理结果以订单状态为准。")
COUNTED = ("pending", "processing", "succeeded")  # ledger states the velocity limit counts
MY_ORDERS_LIMIT = 5
LIVE = ("claiming", "pending", "processing", "succeeded")  # states that hold an order (models.LIVE_REFUND)


def _order_id(args: dict[str, Any]) -> str:
    value = normalize_width(str(args.get("order_id") or "")).strip().upper()
    if not value:
        raise ToolError("缺少订单号")
    if len(value) > 64:  # the ledger's column; no order system issues numbers that long
        raise ToolError("订单号格式不正确")
    return value


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _days_since_delivery(order: OrderInfo, at: datetime) -> int | None:
    """Whole days since delivery, the unit the policy is written in ("自签收之日起7天内");
    None while the order has not been delivered (refunding before delivery is not time-limited)."""
    if order.delivered_at is None:
        return None
    return max(0, (at - _as_utc(order.delivered_at)).days)


def _virtual(order: OrderInfo) -> bool:
    """Memberships, coins, digital albums: they take effect on payment and nothing is shipped
    (an order system marks such items "virtual")."""
    return bool(order.items) and all(item.get("virtual") for item in order.items)


# How the after-sales policy is worded for goods that are shipped, and for what takes effect on
# payment (the windows themselves are the tenant's: return_window_days, quality_window_days).
_SHIPPED_WORDS = {"delivered": "已签收", "since": "签收", "first": "无理由退货", "second": "仅质量问题可以退换"}
_VIRTUAL_WORDS = {"delivered": "已开通", "since": "开通", "first": "无理由退款", "second": "需核实本期权益未使用后才能退款"}


def _words(order: OrderInfo) -> dict[str, str]:
    return _VIRTUAL_WORDS if _virtual(order) else _SHIPPED_WORDS


def _status_text(order: OrderInfo) -> str:
    if order.status == "delivered":
        return _words(order)["delivered"]
    return STATUS_TEXT.get(order.status, order.status)


def _no_online_refund(order: OrderInfo) -> str | None:
    """Why an item cannot be refunded online, when the order system says so (coins, digital albums,
    a subscription billed by an app store): the customer hears the reason, a person handles exceptions."""
    return next((str(item["no_online_refund"]) for item in order.items if item.get("no_online_refund")), None)


def _last_refund_day(tenant: Tenant) -> int:
    """After this many days since delivery no refund can be filed online at all."""
    return max(tenant.return_window_days, tenant.quality_window_days)


def _load_order(ctx: ToolContext, order_id: str) -> OrderInfo:
    # One snapshot per tool call: the precheck, the approval policy and the handler of one refund read
    # the order once. A resumed approval is a new call and reads it afresh.
    seen = reads()
    if seen is not None and ("order", order_id) in seen:
        order = seen[("order", order_id)]
    else:
        try:
            order = ctx.orders.get_order(order_id)
        except GatewayError as exc:  # the details (status, attempts) are in the tool span's "integration"
            raise ToolUnavailable(ORDER_SYSTEM_DOWN_TEXT) from exc
        if seen is not None:
            seen[("order", order_id)] = order
    # Same message for "missing" and "not yours": do not reveal that an order exists. Identity comes
    # from the logged-in user, never from the model or from what the order system was asked.
    if order is None or (not ctx.is_staff and order.customer_ref != customer_ref(ctx.user)):
        raise ToolError(NOT_FOUND_TEXT)
    return order


def _customer_id(ctx: ToolContext, order: OrderInfo) -> int:
    """The account the order belongs to: velocity limits and the refund lock are per customer. Staff
    acting for a customer the order system knows but LoopDesk does not are sent to the back office:
    a refund nobody's limit counts would be one the limit cannot stop."""
    if not ctx.is_staff:
        return ctx.user.id  # _load_order checked the order is theirs
    customer = ctx.db.execute(select(User.id).where(
        User.tenant_id == ctx.tenant.id, func.lower(User.email) == order.customer_ref)).scalar_one_or_none()
    if customer is None:
        raise ToolError("这笔订单的顾客没有本系统的账号，请在订单系统里为其办理退款")
    return customer


def _live_refund(ctx: ToolContext, order_id: str) -> Refund | None:
    return ctx.db.execute(select(Refund).where(
        Refund.tenant_id == ctx.tenant.id, Refund.order_id == order_id, Refund.idempotency_key.is_not(None),
        Refund.status.in_(LIVE),
    ).limit(1)).scalar_one_or_none()


def _held_by(refund: Refund | None) -> str:
    """Why an order with a live refund cannot get another one, as the customer is told. An order has
    one refund through the agent, a partial one included: a further one is a person's call."""
    if refund is None or refund.status in ("claiming", "pending"):
        return "该订单已有一笔退款正在处理，请勿重复提交"
    if refund.status == "succeeded":
        return f"该订单已退款（退款单 #{refund.id}），{ONE_REFUND_TEXT}"
    return f"该订单的退款已受理（退款单 #{refund.id}），{ONE_REFUND_TEXT}"


# ---------------------------------------------------------------- search_knowledge
def search_knowledge(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query 不能为空")
    top_k = max(1, min(int(args.get("top_k") or ctx.settings.retrieval_top_k), 8))
    scope = {"public", "internal"} if ctx.is_staff else {"public"}
    hits, debug = hybrid_search(ctx.db, ctx.tenant.id, query, ctx.embedder, scope, top_k=top_k,
                                reranker=ctx.reranker, deadline=ctx.deadline, **gate_options(ctx.settings))
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
            page=hit.page, relevance=round(hit.relevance, 3), reviewed=hit.reviewed,
        )
        results.append({
            "ref": evidence.ref, "doc": hit.doc_title, "heading": hit.heading,
            "text": hit.text, "relevance": round(hit.relevance, 3), "page": hit.page,
        })
    output: dict[str, Any] = {"query": query, "results": results}
    if debug.vector_error:
        # Distinct from "nothing relevant": the model must not tell the customer the knowledge
        # base has no answer when only part of the search ran (N-078).
        output["degraded"] = "语义检索暂不可用，本次只用关键词检索"
        output["vector_error"] = debug.vector_error
    elif debug.rerank_error:  # the gate lost its semantic signal: a colloquial question may be missed
        output["degraded"] = "重排服务暂不可用，本次只按关键词和向量检索"
        output["rerank_error"] = debug.rerank_error
    if not results:
        output["note"] = ("检索服务部分故障，这次没有查到，不代表知识库里没有；请顾客稍后再试或转人工"
                          if debug.vector_error or debug.rerank_error or debug.rerank_skipped
                          else "知识库中没有足够相关的内容")
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
        "vector_error": debug.vector_error,
        "rerank_error": debug.rerank_error,
        "rerank_ms": debug.rerank_ms,
        "rerank_skipped": debug.rerank_skipped,
        "hits": [h.as_dict() for h in hits],
    }
    return output


REFUND_STATUS_TEXT = {
    "pending": "已提交，等待支付系统确认",
    "processing": "已受理，正在退回原支付方式",
    "succeeded": "已退款成功",
    "failed": "未能完成",
}


def _refund_progress(ctx: ToolContext, order_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The refunds filed for an order and a request still waiting for a person, so "退款到哪一步了"
    is answered from the ledger, read-only, instead of filing another refund (N-056). The caller has
    already checked the order is the customer's; the approval's reason (risk controls) stays out."""
    zone = zone_of(ctx.tenant)
    rows = ctx.db.execute(select(Refund).where(
        Refund.tenant_id == ctx.tenant.id, Refund.order_id == order_id, Refund.status != "claiming",
    ).order_by(Refund.id.desc()).limit(5)).scalars()
    refunds = [{
        "refund_id": r.id, "amount": round(r.amount, 2), "status": r.status,
        "status_text": REFUND_STATUS_TEXT.get(r.status, r.status),
        "filed_on": local_date(r.created_at, zone) if r.created_at else None,
    } for r in rows]
    waiting = next((a for a in ctx.db.execute(select(Approval).where(
        Approval.tenant_id == ctx.tenant.id, Approval.tool_name == "request_refund", Approval.status == "pending",
    ).order_by(Approval.id.desc())).scalars() if a.arguments.get("order_id") == order_id), None)
    approval = None if waiting is None else {
        "approval_id": waiting.id, "amount": round(float(waiting.arguments.get("amount") or 0), 2),
        "status_text": "等待人工审核",
        "filed_on": local_date(waiting.created_at, zone) if waiting.created_at else None,
    }
    return refunds, approval


# ---------------------------------------------------------------- orders
def get_order(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    order = _load_order(ctx, _order_id(args))
    status_text = _status_text(order)
    refundable = order.status in REFUNDABLE
    refund_note = "" if refundable else f"该订单当前状态为“{status_text}”，不能重复申请退款"
    if refundable and _no_online_refund(order):
        refundable, refund_note = False, _no_online_refund(order)
    live = _live_refund(ctx, order.order_id) if refundable else None
    if live is not None:  # filed here, and the order system does not show it (yet, or keeps partly refunded orders open)
        refundable, refund_note = False, _held_by(live).removesuffix("，请勿重复提交")
    days = _days_since_delivery(order, datetime.now(timezone.utc))
    if refundable and days is not None:
        tenant, words = ctx.tenant, _words(order)
        if days > _last_refund_day(tenant):
            refundable = False
            refund_note = f"订单已{words['since']} {days} 天，超过 {_last_refund_day(tenant)} 天的售后期限"
        elif days > tenant.return_window_days:
            refund_note = (f"订单已{words['since']} {days} 天，已超过 {tenant.return_window_days} 天{words['first']}期限，"
                           f"{tenant.quality_window_days} 天内{words['second']}，提交后需要人工核实")
    refunds, waiting = _refund_progress(ctx, order.order_id)
    return {
        "order_id": order.order_id,
        "status": order.status,
        "status_text": status_text,
        "amount": round(order.amount, 2),
        "items": order.items,
        # Local dates: a delivery at 06:30 Beijing time is 22:30 UTC the day before (N-024).
        "paid_at": local_date(order.paid_at, zone_of(ctx.tenant)) if order.paid_at else None,
        "delivered_at": local_date(order.delivered_at, zone_of(ctx.tenant)) if order.delivered_at else None,
        "days_since_delivery": days,
        "region": order.region,
        "refundable": refundable,
        "refund_note": refund_note,
        "refunds": refunds,  # newest first; a progress question is answered from these, never by filing one
        "refund_awaiting_approval": waiting,
        "summary": f"订单 {order.order_id} 当前状态为“{status_text}”，订单金额 ¥{order.amount:.2f}",
    }


def list_orders(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """A customer's recent orders: staff name the customer by e-mail, a customer gets their own
    whatever they name. Not one of the agent's tools; the MCP server offers it to staff tools."""
    customer = (str(args.get("customer_email") or "").strip().lower() if ctx.is_staff
                else customer_ref(ctx.user))
    if not customer:
        raise ToolError("缺少顾客邮箱")
    try:
        orders = ctx.orders.list_orders(customer, limit=10)
    except GatewayError as exc:
        raise ToolUnavailable(ORDER_SYSTEM_DOWN_TEXT) from exc
    zone = zone_of(ctx.tenant)
    return {"customer": customer, "orders": [{
        "order_id": o.order_id, "status_text": _status_text(o), "amount": round(o.amount, 2),
        "paid_at": local_date(o.paid_at, zone) if o.paid_at else None, "items": [i["name"] for i in o.items],
    } for o in orders]}


def list_my_orders(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """The signed-in customer's own recent orders, newest first (N-057): most people do not know
    their order number, they say "my parcel" or "the robot vacuum I bought". Always the caller's
    own orders, whatever the arguments say; read-only, so the agent may follow up on a single
    match, but it asks before filing a refund on an order it picked."""
    try:
        orders = ctx.orders.list_orders(customer_ref(ctx.user), limit=MY_ORDERS_LIMIT)
    except GatewayError as exc:
        raise ToolUnavailable(ORDER_SYSTEM_DOWN_TEXT) from exc
    zone = zone_of(ctx.tenant)
    return {"orders": [{
        "order_id": o.order_id, "status": o.status, "status_text": _status_text(o), "amount": round(o.amount, 2),
        "paid_at": local_date(o.paid_at, zone) if o.paid_at else None,
        "items": [str(i.get("name", "")) for i in o.items],
    } for o in orders[:MY_ORDERS_LIMIT]]}


def track_shipment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    order = _load_order(ctx, _order_id(args))
    try:
        shipment = ctx.orders.get_shipment(order.order_id) if order.status != "paid" else None
    except GatewayError as exc:
        raise ToolUnavailable(ORDER_SYSTEM_DOWN_TEXT) from exc
    if _virtual(order):
        names = "、".join(str(item.get("name", "")) for item in order.items)
        return {"order_id": order.order_id, "shipped": False, "virtual": True,
                "message": f"订单 {order.order_id}（{names}）是虚拟商品，付款后即时生效，没有物流信息。"}
    if shipment is None or not shipment.events:
        return {"order_id": order.order_id, "shipped": False, "message": f"订单 {order.order_id} 目前还未发货。"}
    latest = shipment.events[-1]
    return {
        "order_id": order.order_id,
        "shipped": True,
        "carrier": shipment.carrier,
        "tracking_no": shipment.tracking_no,
        "events": shipment.events,
        "message": (
            f"订单 {order.order_id} 由{shipment.carrier}承运，运单号 {shipment.tracking_no}。"
            f"最新进度（{latest['time']}）：{latest['desc']}。"
        ),
    }


# ---------------------------------------------------------------- refunds (high risk)
def _refund_of(db: Session, approval_id: int) -> Refund | None:
    return db.execute(select(Refund).where(Refund.approval_id == approval_id)).scalar_one_or_none()


def _refund_for_approval(ctx: ToolContext) -> Refund | None:
    """An approval files one refund: retrying an approved run finds its ledger row (and the
    row's idempotency key) instead of filing another."""
    if ctx.approval_id is None or ctx.dry_run:
        return None
    return _refund_of(ctx.db, ctx.approval_id)


def _refund_executed(db: Session, approval_id: int) -> dict[str, Any] | None:
    """The refund an approval already filed, as request_refund reports it (ToolDef.executed). A
    ledger row is there exactly when the refund was filed: it commits before the payment system is
    asked, so a refund still waiting for its answer counts (it may have been made). Asked again, it
    goes out under the row's key and is made once."""
    refund = _refund_of(db, approval_id)
    return _refund_result(refund, "approved") if refund is not None else None


def _refusal(code: str) -> str:
    return REFUSAL_TEXT.get(code, REFUSED_TEXT)


def _refund_result(refund: Refund, status: str) -> dict[str, Any]:
    """``status`` is the decision (auto_approved / approved); ``refund_status`` is where the money is."""
    result = {"status": status, "refund_status": refund.status, "refund_id": refund.id,
              "order_id": refund.order_id, "amount": refund.amount}
    if refund.status == "pending":
        result["message"] = PENDING_TEXT.format(id=refund.id)
    elif refund.status == "failed":
        result["message"] = f"退款单 #{refund.id} 未能完成：{_refusal(refund.error)}。"
    elif refund.status == "succeeded":
        result["message"] = f"退款单 #{refund.id} 已退款成功，¥{refund.amount:.2f} 已按原支付方式退回。"
    else:
        result["message"] = f"退款单 #{refund.id} 已受理，退款金额 ¥{refund.amount:.2f} 将按原支付方式退回。"
    return result


def refund_request(refund: Refund) -> RefundRequest:
    """The request a ledger row stands for, built from columns that never change: every attempt (the
    first, the gateway's retries, a retried approval, app.ops.refund_reconcile) sends the same one."""
    return RefundRequest(
        idempotency_key=refund.idempotency_key, order_id=refund.order_id, amount=refund.amount,
        reason=refund.reason, approval_id=refund.approval_id, loopdesk_refund_id=refund.id,
    )


def file_refund(db: Session, gateway: RefundGateway, refund: Refund) -> None:
    """Ask the payment system for the refund a ledger row records, and write its answer on the row
    (the caller commits). No answer leaves the row pending: it keeps the order held, and the same
    request goes out again later. ``error`` holds a refusal's contract code, or "unanswered: …"."""
    try:
        result = gateway.create_refund(refund_request(refund))
    except RefundRejected as exc:  # nothing was paid: the order is free again
        values = {"status": "failed", "error": exc.code[:255]}
    except GatewayError as exc:
        values = {"error": f"unanswered: {exc.detail}"[:255]}
    else:
        values = {"status": result.status, "external_id": result.refund_id,
                  "error": "failed" if result.status == "failed" else ""}
    # Only a row still waiting for its answer takes this one. Two attempts can hold the same row (the
    # reconciliation job and a retried approval); under one key the payment system answers both alike,
    # but the demo order table refuses the second, and that refusal must not undo the refund the first
    # one made. The row as it now stands is what the caller reports.
    db.execute(update(Refund).where(Refund.id == refund.id, Refund.status == "pending")
               .values(**values, attempts=Refund.attempts + 1, updated_at=datetime.now(timezone.utc))
               .execution_options(synchronize_session=False))
    db.refresh(refund)


def _application_time(ctx: ToolContext) -> datetime:
    """When the customer asked. After a human approval the tool runs again, possibly days
    later; the after-sales window is judged at the request, so a slow review queue cannot
    push a valid request out of its window."""
    approval = ctx.db.get(Approval, ctx.approval_id) if ctx.approval_id is not None else None
    return _as_utc(approval.created_at) if approval is not None else datetime.now(timezone.utc)


def _validated(ctx: ToolContext, args: dict[str, Any]) -> tuple[OrderInfo, float]:
    """The order as the order system has it now, and the amount that would be paid."""
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
        raise ToolError(f"该订单当前状态为“{_status_text(order)}”，不能发起退款")
    if _no_online_refund(order):
        raise ToolError(_no_online_refund(order))
    days = _days_since_delivery(order, _application_time(ctx))
    if days is not None and days > _last_refund_day(ctx.tenant):
        # A hard rule, not an approval: the policy leaves nothing to decide. Exceptions
        # (goodwill, warranty) belong to a human in the back office, not to the agent.
        raise ToolError(f"订单已{_words(order)['since']} {days} 天，超过 {_last_refund_day(ctx.tenant)} 天的售后期限，"
                        "不能在线申请退款；如有特殊情况，可以转人工客服处理")
    if ctx.approval_id is None:
        pending = ctx.db.execute(select(Approval).where(
            Approval.tenant_id == ctx.tenant.id, Approval.tool_name == "request_refund", Approval.status == "pending",
        )).scalars()
        if any(a.arguments.get("order_id") == order.order_id for a in pending):
            raise ToolError("该订单已有一笔退款申请在等待人工审核，请勿重复提交")
    return order, amount


def _refund_precheck(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    done = _refund_for_approval(ctx)
    if done is not None:  # already filed; the order is held now, so skip re-validation
        return {"order_id": done.order_id, "amount": done.amount}
    # Whatever the model makes of it, "先别退款" or "如果退款的话…" does not move money (N-091). A turn that
    # does not mention refunds at all ("是的，退吧" after the model asked) is left to the model.
    if ctx.approval_id is None and refund_declined(ctx.customer_text):
        raise ToolError("顾客这句话是在咨询退款或说了先不退，并没有申请退款，不能发起退款。"
                        "请回答顾客的问题；顾客明确要求退款时再申请。")
    order, amount = _validated(ctx, args)
    return {"order_id": order.order_id, "amount": amount}


def _refund_history(ctx: ToolContext, customer_id: int) -> tuple[int, float]:
    """Refunds filed for this customer inside the velocity window, wherever their orders live; a
    refund the payment system refused paid nothing and does not count."""
    since = datetime.now(timezone.utc) - timedelta(days=ctx.settings.refund_window_days)
    count, total = ctx.db.execute(
        select(func.count(Refund.id), func.coalesce(func.sum(Refund.amount), 0.0))
        .where(Refund.tenant_id == ctx.tenant.id, Refund.customer_id == customer_id, Refund.created_at >= since,
               Refund.status.in_(COUNTED))
    ).one()
    return int(count), float(total)


def _velocity_reason(ctx: ToolContext, customer_id: int, amount: float) -> str | None:
    """Velocity check, counted per customer (not per operator: a support agent filing on the
    customer's behalf must not reset the counter). It stops "many small refunds". When it fires,
    the reviewer gets the history that triggered it in the evidence package."""
    settings, window = ctx.settings, ctx.settings.refund_window_days
    count, total = _refund_history(ctx, customer_id)
    reason = None
    if count + 1 > settings.refund_window_max_count:
        reason = (f"近 {window} 天已有 {count} 笔退款，超过自动审批的次数上限"
                  f"（{settings.refund_window_max_count} 笔）")
    elif total + amount > settings.refund_window_max_amount + 1e-6:
        reason = (f"近 {window} 天累计退款将达到 ¥{total + amount:.2f}，"
                  f"超过 ¥{settings.refund_window_max_amount:.2f} 的累计自动审批上限")
    if reason:
        ctx.add_evidence("tool", "refund_history", json.dumps({
            "customer_id": customer_id, "window_days": window, "refund_count": count,
            "refund_total": round(total, 2), "this_request": amount,
        }, ensure_ascii=False))
    return reason


def _refund_policy(ctx: ToolContext, args: dict[str, Any]) -> ApprovalNeed | None:
    # args went through _refund_precheck, so "amount" is the amount that would be paid.
    amount = round(float(args["amount"]), 2)
    order = _load_order(ctx, _order_id(args))
    tenant, settings = ctx.tenant, ctx.settings
    reasons: list[str] = []
    customer_note = ""

    # Between the no-reason and the quality window only a quality problem qualifies, and
    # the tool cannot look at the customer's photos: a person decides, whatever the amount.
    days = _days_since_delivery(order, datetime.now(timezone.utc))
    if days is not None and days > tenant.return_window_days:
        words = _words(order)
        customer_note = (f"订单已{words['since']} {days} 天，超过 {tenant.return_window_days} 天{words['first']}期限，"
                         f"{tenant.quality_window_days} 天内{words['second']}")
        reasons.append(customer_note)  # published policy, so the customer may hear it too
        ctx.add_evidence("tool", "after_sales_window", json.dumps({
            "order_id": order.order_id, "delivered_at": local_date(order.delivered_at, zone_of(tenant)),
            "days_since_delivery": days, "return_window_days": tenant.return_window_days,
            "quality_window_days": tenant.quality_window_days,
        }, ensure_ascii=False))

    limit = tenant.auto_refund_limit
    if amount > limit:
        reasons.append(f"超过自动审批额度 ¥{limit:.2f}")
    else:
        velocity = _velocity_reason(ctx, _customer_id(ctx, order), amount)
        if velocity:
            reasons.append(velocity)
    # Amount and velocity limits are risk controls: the reviewer sees them, the customer does not.
    return ApprovalNeed("；".join(reasons), customer_note) if reasons else None


def _settle(ctx: ToolContext, refund: Refund, status: str) -> dict[str, Any]:
    """File the refund with the payment system and say where it stands. A refusal is a tool error
    (the customer gets the reason, the trace the payment system's code and message)."""
    file_refund(ctx.db, ctx.refunds, refund)
    ctx.db.commit()
    if refund.status == "failed":
        raise ToolError(_refusal(refund.error))
    return _refund_result(refund, status)


def request_refund(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    done = _refund_for_approval(ctx)
    if done is not None:
        if done.status == "pending":  # filed, never answered: ask again, under the same key
            return _settle(ctx, done, "approved")
        if done.status == "failed":
            raise ToolError(_refusal(done.error))
        return _refund_result(done, "approved")
    # Re-validate at execution time: the order may have changed while waiting for approval.
    order, amount = _validated(ctx, args)
    status = "approved" if ctx.approval_id else "auto_approved"
    if ctx.dry_run:
        return {
            "status": status, "refund_id": None, "order_id": order.order_id, "amount": amount,
            "message": f"（演练）退款申请已受理，退款金额 ¥{amount:.2f} 将按原支付方式退回。",
        }
    customer_id = _customer_id(ctx, order)
    # The claim, the lock and the re-check run in a savepoint: undoing them must not undo what the
    # run has not committed yet (its model and route; with buffered trace writes nothing is
    # committed until the run's own commit points). Reviewed after N-039.
    try:
        with ctx.db.begin_nested():
            # Claim the order by writing its ledger row. The checks above are reads: two requests for
            # the same order (a double click, a retry) can both pass them, and both used to pay out.
            # The partial unique index lets one live refund per order exist: on SQLite the second
            # insert waits for the first transaction and fails, on PostgreSQL it waits on the index
            # entry and fails once the first commits. It is the first write in the savepoint, and no
            # read comes before it, so on SQLite the write lock is taken with a fresh snapshot (by this
            # insert, or by the flush of the run's pending changes that opening the savepoint
            # triggers); a read first could not have been upgraded to a write. "claiming" keeps the
            # row out of the velocity count below; nobody else ever sees that state.
            refund = Refund(
                tenant_id=ctx.tenant.id, order_id=order.order_id, customer_id=customer_id, amount=amount,
                reason=str(args.get("reason") or "")[:500], approval_id=ctx.approval_id, status="claiming",
                conversation_id=ctx.conversation_id,
                idempotency_key=f"ld-{uuid.uuid4().hex}",  # never reused: not a sequence number (N-028)
            )
            ctx.db.add(refund)
            ctx.db.flush()
            # Refunds for one customer run one at a time from here: the velocity limit below must
            # count the refunds of requests that passed the same policy read concurrently (N-039).
            # PostgreSQL locks the customer's row FOR NO KEY UPDATE, which queues refunds for that
            # customer but not inserts that merely reference them (a ticket, a message); SQLite
            # already has one writer at a time. Every refund takes the order, then the customer.
            ctx.db.execute(select(User.id).where(User.id == customer_id).with_for_update(key_share=True))
            if ctx.approval_id is None:  # an automatic refund: the velocity limit again, under the lock
                velocity = _velocity_reason(ctx, customer_id, amount)
                if velocity:  # the savepoint's rollback removes the row: the order is free again
                    raise ApprovalRequired(ApprovalNeed(velocity))
            refund.status = "pending"
    except IntegrityError:
        ctx.db.commit()  # keep what the run had pending, and end the transaction (SQLite's write lock)
        raise ToolError(_held_by(_live_refund(ctx, order.order_id))) from None
    except (ToolError, ApprovalRequired):
        ctx.db.commit()
        raise
    # The row and the claim are durable before the payment system is asked: from here on, whatever
    # happens to this process, the refund is filed once, under this key (app.ops.refund_reconcile).
    ctx.db.commit()
    return _settle(ctx, refund, status)


WITHDRAWN_COMMENT = "顾客已撤回申请"


def _end_paused_run(ctx: ToolContext, approval: Approval, now: datetime) -> None:
    """The run that filed the application ends here: nothing will resume it, so its paused call goes. A
    customer who withdraws from another conversation (N-127) sees the outcome in the one that said "under
    review" too, which is also what stops that page from polling for a decision."""
    run = ctx.db.get(Run, approval.run_id)
    if run is None or run.status != "waiting_approval":
        return
    amount = float((approval.arguments or {}).get("amount") or 0)
    text = f"您已撤回订单 {(approval.arguments or {}).get('order_id')} 的退款申请（¥{amount:.2f}），这笔款项不会退回。"
    state = dict(run.state or {})
    tools = list(state.get("tools", []))
    state.pop("pending", None)
    state.pop("messages", None)
    run.state, run.status, run.finished_at, run.output_text = state, "completed", now, text
    if run.conversation_id is not None and run.conversation_id != ctx.conversation_id:
        ctx.db.add(Message(tenant_id=run.tenant_id, conversation_id=run.conversation_id, role="assistant", content=text,
                           run_id=run.id, meta={"citations": [], "verification": None, "tools": tools,
                                                "status": "completed", "withdrawn": True}))
        conversation = ctx.db.get(Conversation, run.conversation_id)
        if conversation is not None:
            conversation.updated_at = now


def withdraw_refund_request(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """The customer takes back their own refund application while it waits for review (N-127).
    No money moves: the approval leaves the queue as "withdrawn", its paused run ends, and a reviewer
    who opens it afterwards gets 409 instead of paying out what the customer called off. A refund
    already filed with the payment system is not withdrawn here: that is a person's job."""
    order = _load_order(ctx, _order_id(args))
    text = normalize_width(ctx.customer_text)
    # A turn that files or chases a refund ("我要退款", "SO… 退了吧", "退款到哪了") never takes one back, whatever
    # the model makes of it; "撤回退款申请", "不想退了" and a bare "是的" after the model asked may.
    if not refund_withdraw_requested(text) and (
            refund_requested(text) or (REFUND_RE.search(text) and not refund_bare_decline(text))):
        raise ToolError("顾客这句话没有要求撤回退款申请，不能撤回。请回答顾客的问题。")
    mine = ctx.db.execute(select(Approval).join(Run, Run.id == Approval.run_id).where(
        Approval.tenant_id == ctx.tenant.id, Approval.tool_name == "request_refund", Approval.status == "pending",
        Run.user_id == ctx.user.id,
    ).order_by(Approval.id)).scalars().all()
    waiting = [a for a in mine if (a.arguments or {}).get("order_id") == order.order_id]
    if not waiting:
        live = _live_refund(ctx, order.order_id)
        message = (f"订单 {order.order_id} 没有等待审核的退款申请，无需撤回。" if live is None else
                   f"订单 {order.order_id} 的退款已提交到支付系统（退款单 #{live.id}），无法在线撤回；如需处理请转人工客服。")
        return {"withdrawn": False, "order_id": order.order_id, "refund_id": None if live is None else live.id,
                "message": message}
    amount = round(sum(float((a.arguments or {}).get("amount") or 0) for a in waiting), 2)
    if ctx.dry_run:
        return {"withdrawn": False, "order_id": order.order_id, "amount": amount,
                "message": f"（演练）将撤回订单 {order.order_id} 待审核的退款申请 ¥{amount:.2f}。"}
    now, done = datetime.now(timezone.utc), []
    for approval in waiting:
        # Conditional, like a reviewer's claim: one decided a moment ago stays decided.
        if ctx.db.execute(update(Approval).where(Approval.id == approval.id, Approval.status == "pending")
                          .values(status="withdrawn", reviewer_id=None, comment=WITHDRAWN_COMMENT, decided_at=now),
                          execution_options={"synchronize_session": False}).rowcount != 1:
            continue
        _end_paused_run(ctx, approval, now)
        done.append(approval)
    for approval in done:  # audit last: the tenant row's counter is the last lock, as everywhere else
        args_ = approval.arguments or {}
        audit.record(ctx.db, Principal(ctx.user, ctx.tenant), "approval.withdraw", f"approval:{approval.id}",
                     tool=approval.tool_name, order_id=args_.get("order_id"), amount=args_.get("amount"),
                     comment=WITHDRAWN_COMMENT)
    ctx.db.commit()
    if not done:  # a reviewer got there first
        return {"withdrawn": False, "order_id": order.order_id,
                "message": f"订单 {order.order_id} 的退款申请刚刚已经审核完毕，无法撤回；结果会在对话里通知您。"}
    amount = round(sum(float((a.arguments or {}).get("amount") or 0) for a in done), 2)
    return {"withdrawn": True, "order_id": order.order_id, "amount": amount, "approval_ids": [a.id for a in done],
            "message": f"已为您撤回订单 {order.order_id} 待审核的退款申请（¥{amount:.2f}），这笔款项不会退回；"
                       "以后如需退款，可以重新申请。"}


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
    title, description = handoff.brief(reason, ctx.customer_text, handoff.earlier_orders(ctx.db, ctx.conversation_id))
    if not ctx.dry_run:
        if ctx.conversation_id:
            conversation = ctx.db.get(Conversation, ctx.conversation_id)
            if conversation:
                conversation.status = "handoff"
        ctx.db.add(Ticket(
            tenant_id=ctx.tenant.id, conversation_id=ctx.conversation_id, customer_id=ctx.user.id,
            kind="handoff", title=title, description=description, priority="high",
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
        description=("按订单号查询订单状态、金额、商品、是否可退款，以及该订单已有的退款记录和等待审核的退款申请。"
                     "顾客问退款进度时用它回答，不要再发起退款。顾客只能查询自己的订单。"),
        parameters={
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "订单号，如 SO20261008002"}},
            "required": ["order_id"],
        },
        handler=get_order,
    ))
    registry.register(ToolDef(
        name="list_my_orders",
        description=("列出当前顾客自己最近的订单（订单号、状态、金额、商品，最多 5 笔）。顾客没有提供订单号、"
                     "只说“我的快递”“我买的那个扫地机器人”时先调用它：只有一笔对得上就继续查这一笔；"
                     "多笔时把订单列给顾客，请顾客确认是哪一单。不要替顾客猜订单去申请退款。"),
        parameters={"type": "object", "properties": {}},
        handler=list_my_orders,
        roles=("customer",),  # staff look a customer up by e-mail (list_orders, MCP), not through their own account
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
        description="为订单发起退款，只在顾客明确要求退款时调用（问退款进度、到账时间时不要调用）。金额超过租户自动审批额度时会进入人工审批，审批结果出来前不要向用户承诺退款成功。",
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
        executed=_refund_executed,
    ))
    registry.register(ToolDef(
        name="withdraw_refund_request",
        description=("撤回顾客自己还在等待人工审核的退款申请（不会退钱）。只在顾客明确说撤回、取消退款申请或不想退了时调用；"
                     "已提交到支付系统的退款不能撤回，按返回的提示告知顾客。"),
        parameters={
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "订单号"}},
            "required": ["order_id"],
        },
        handler=withdraw_refund_request,
        risk="write",
        roles=("customer",),  # staff decide an application in the approval queue
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
